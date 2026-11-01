#!/usr/bin/env python3
"""Exploratory local Wi-Fi fingerprint clustering; never invents GPS labels."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform

THRESHOLD=0.72
MIN_SHARED=3
MIN_NETWORKS=5
MIN_CLUSTER=3


def load(path):
    scans=[];seen={};anchors={}
    for line in Path(path).read_text().splitlines():
        if not line.strip():continue
        r=json.loads(line)
        if r.get('type')=='clock_anchor':anchors.setdefault((r['device'],r['session']),[]).append(r)
        if r.get('type')!='scan' or r['result']<0:continue
        key=(r['device'],r['session'],r['scan_id'])
        payload=json.dumps(r,sort_keys=True)
        if key in seen:
            if seen[key]!=payload:raise ValueError('Conflicting scan identity')
            continue
        seen[key]=payload;scans.append(r)
    return scans,anchors


def filter_aps(scans, min_rssi=None, filter_laa=False):
    """Filters noisy, mobile, or randomized MAC addresses."""
    if min_rssi is None and not filter_laa: return scans
    cleaned = []
    for r in scans:
        aps = []
        for a in r.get('aps', []):
            if min_rssi is not None and a['rssi_dbm'] < min_rssi:
                continue
            if filter_laa:
                first = int(a['bssid'].split(':')[0], 16)
                if first & 0x02:
                    continue
            aps.append(a)
        cleaned.append(dict(r, aps=aps, result=len(aps)))
    return cleaned


def similarity(left,right=None):
    if right is None:right=left
    vocab=sorted({a['bssid'] for r in left+right for a in r['aps']})
    idx={b:i for i,b in enumerate(vocab)}
    def encode(rows):
        values=np.zeros((len(rows),len(vocab)))
        presence=np.zeros_like(values,dtype=np.int32)
        for i,r in enumerate(rows):
            for ap in r['aps']:
                j=idx[ap['bssid']];presence[i,j]=1
                values[i,j]=max(values[i,j],np.clip((ap['rssi_dbm']+100)/60,0.05,1))
        norm=np.linalg.norm(values,axis=1,keepdims=True)
        return np.divide(values,norm,out=np.zeros_like(values),where=norm>0),presence
    x,a=encode(left);y,b=encode(right)
    with np.errstate(all='ignore'):
        cosine=np.clip(x@y.T,0,1)
    shared=a@b.T;union=a.sum(1)[:,None]+b.sum(1)[None,:]-shared
    jaccard=np.divide(shared,union,out=np.zeros_like(cosine),where=union>0)
    score=.7*cosine+.3*jaccard
    eligible=(shared>=MIN_SHARED)&(a.sum(1)[:,None]>=MIN_NETWORKS)&(b.sum(1)[None,:]>=MIN_NETWORKS)
    return np.where(eligible,score,0),cosine,jaccard,shared


def cluster(scans,threshold=THRESHOLD,method='complete'):
    n=len(scans)
    if n==0:return np.zeros(0,dtype=int),np.zeros((0,0))
    score,*_=similarity(scans)
    if n==1:return np.array([-1]),score
    distance=np.clip(1-score,0,1);np.fill_diagonal(distance,0)
    raw=fcluster(linkage(squareform(distance,checks=False),method=method),t=1-threshold,criterion='distance')
    labels=np.full(n,-1,dtype=int);next_label=1
    for c in sorted(set(raw),key=lambda c:np.flatnonzero(raw==c)[0]):
        members=np.flatnonzero(raw==c)
        if len(members)>=MIN_CLUSTER:
            labels[members]=next_label;next_label+=1
    return labels,score


def predict(train,labels,queries,threshold=THRESHOLD,margin=0.08):
    scores,_,_,shared=similarity(queries,train)
    result=[]
    for i,row in enumerate(scores):
        ranked=[]
        for c in sorted(set(labels)-{-1}):
            candidates=np.flatnonzero(labels==c)
            best=candidates[np.argsort(row[candidates])[-3:]]
            ranked.append((float(np.mean(row[best])),int(c),best))
        ranked.sort(key=lambda x:x[0],reverse=True)
        if not ranked:result.append(dict(place=-1,score=0,margin=None,shared=0));continue
        first=ranked[0];gap=first[0]-ranked[1][0] if len(ranked)>1 else None
        accepted=first[0]>=threshold and (gap is None or gap>=margin)
        result.append(dict(place=first[1] if accepted else -1,score=first[0],margin=gap,shared=int(np.max(shared[i,first[2]]))))
    return result


def analyze(path,out,threshold=THRESHOLD,min_rssi=None,filter_laa=False,method='complete',drop_mobile=False):
    scans,anchors=load(path)
    if drop_mobile:
        import anchors as aps
        classes=aps.classify(scans,anchors)
        scans=[dict(r,aps=[a for a in r['aps'] if classes.get(a['bssid'].upper(),{}).get('class')!='mobile']) for r in scans]
        scans=[dict(r,result=len(r['aps'])) for r in scans]
    if min_rssi is not None or filter_laa:
        scans = filter_aps(scans, min_rssi=min_rssi, filter_laa=filter_laa)
    labels,score=cluster(scans,threshold=threshold,method=method);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    groups=[]
    for c in sorted(set(labels)-{-1}):
        ids=np.flatnonzero(labels==c);sub=score[np.ix_(ids,ids)];pairs=sub[np.triu_indices(len(ids),1)]
        groups.append(dict(place=int(c),scans=len(ids),sessions=len({scans[i]['session'] for i in ids}),minimum_pair_similarity=float(pairs.min()),median_pair_similarity=float(np.median(pairs))))
    with (out/'scan-assignments.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['session','scan_id','start_ms','estimated_utc_ms','clock_anchor_available','network_count','candidate_place'])
        for r,c in zip(scans,labels):
            aa=anchors.get((r['device'],r['session']),[])
            anchor=min(aa,key=lambda x:abs(x['ms']-r['start_ms'])) if aa else None
            utc=anchor['utc_ms']+r['start_ms']-anchor['ms'] if anchor else ''
            w.writerow([r['session'],r['scan_id'],r['start_ms'],utc,bool(anchor),len(r['aps']),int(c) if c!=-1 else 'unknown'])
    sensitivity=[]
    for t in [.65,.70,.72,.75,.80,.85]:
        lab,_=cluster(scans,t,method=method);sensitivity.append(dict(threshold=t,clusters=len(set(lab)-{-1}),unassigned=int(sum(lab==-1))))
    holdout=[]
    for session in dict.fromkeys(r['session'] for r in scans):
        train=[r for r in scans if r['session']!=session];query=[r for r in scans if r['session']==session]
        labs,_=cluster(train,threshold=threshold,method=method);pred=predict(train,labs,query,threshold=threshold)
        holdout.append(dict(session=session,queries=len(query),accepted=sum(r['place']!=-1 for r in pred),median_match_score=float(np.median([r['score'] for r in pred]))))
    # Perturbations only test stability against noise, NOT geographic accuracy.
    rng=np.random.default_rng(20261029);perturbed=[]
    for r in scans:
        aps=[dict(a,rssi_dbm=int(np.clip(a['rssi_dbm']+rng.normal(0,4),-127,0))) for a in r['aps'] if rng.random()>=.15]
        perturbed.append(dict(r,aps=aps,result=len(aps)))
    predictions=predict(scans,labels,perturbed,threshold=threshold)
    eligible=[i for i,c in enumerate(labels) if c!=-1]
    unchanged=sum(predictions[i]['place']==labels[i] for i in eligible)
    report=dict(source=Path(path).name,source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),scans=len(scans),unique_bssids=len({a['bssid'] for r in scans for a in r['aps']}),sessions=len({r['session'] for r in scans}),scans_without_clock_anchor=sum(not anchors.get((r['device'],r['session'])) for r in scans),parameters=dict(threshold=threshold,linkage_method=method,min_rssi=min_rssi,filter_laa=filter_laa,drop_mobile=drop_mobile,minimum_shared=MIN_SHARED,minimum_networks=MIN_NETWORKS,minimum_cluster=MIN_CLUSTER,cosine_weight=.7,jaccard_weight=.3,anchor_weights='neutral: insufficient multi-day stability evidence'),candidate_places=groups,unassigned=int(sum(labels==-1)),threshold_sensitivity=sensitivity,leave_one_session_out=holdout,perturbation_test=dict(seed=20261029,rssi_noise_std_db=4,drop_probability=.15,evaluated=len(eligible),same_candidate_place=int(unchanged),rejected=int(sum(predictions[i]['place']==-1 for i in eligible)),different_candidate_place=int(sum(predictions[i]['place'] not in (-1,labels[i]) for i in eligible)),description='Synthetic perturbations of observed scans; not independent ground truth'),gps_accuracy_tested=False,physical_place_accuracy_tested=False)
    (out/'analysis.json').write_text(json.dumps(report,indent=2)+'\n')
    (out/'similarity-matrix.json').write_text(json.dumps(score.round(5).tolist()))
    print(json.dumps(report,indent=2));return report


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('input')
    p.add_argument('--out',required=True)
    p.add_argument('--threshold',type=float,default=THRESHOLD)
    p.add_argument('--min-rssi',type=int,default=None,help="Ignore APs below this RSSI dBm (e.g. -82)")
    p.add_argument('--filter-laa',action='store_true',help="Filter randomized/mobile hotspot MACs")
    p.add_argument('--drop-mobile',action='store_true',help="Drop access points classified as mobile by anchors.py")
    p.add_argument('--method',default='complete',choices=['complete','average','weighted','centroid'],help="Clustering linkage method")
    a=p.parse_args()
    analyze(a.input,a.out,threshold=a.threshold,min_rssi=a.min_rssi,filter_laa=a.filter_laa,method=a.method,drop_mobile=a.drop_mobile)
