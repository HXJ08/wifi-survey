#pragma once
// Portable, per-record lossless LZSS codec. No external dependencies.
// Each independent frame: WSC1 (compressed) or WSR1 (raw), uint32 LE
// uncompressed length, stored length, raw CRC32, then payload.
#include <stdint.h>
#include <stddef.h>
#include <string>
#include <vector>
#include <algorithm>
namespace CompactLog {
constexpr size_t HEADER=16, MAX_RECORD=65536;
inline uint32_t crc(const uint8_t *p,size_t n){uint32_t c=0xffffffff;for(size_t i=0;i<n;i++){c^=p[i];for(int j=0;j<8;j++)c=(c>>1)^((c&1)?0xedb88320UL:0);}return c^0xffffffff;}
inline uint32_t get32(const uint8_t *p){return uint32_t(p[0])|(uint32_t(p[1])<<8)|(uint32_t(p[2])<<16)|(uint32_t(p[3])<<24);}
inline void put32(std::string &s,uint32_t n){for(int i=0;i<4;i++)s.push_back(char(n>>(8*i)));}
inline unsigned hash3(const std::string &s,size_t i){return ((uint8_t(s[i])*251U+uint8_t(s[i+1]))*251U+uint8_t(s[i+2]))&4095;}
inline std::string pack(const std::string &s){
  std::vector<int32_t> head(4096,-1);std::string out;out.reserve(s.size());size_t i=0;
  while(i<s.size()){
    size_t flagpos=out.size();out.push_back(0);uint8_t flags=0;
    for(unsigned bit=0;bit<8&&i<s.size();bit++){
      size_t length=0,distance=0;
      if(i+2<s.size()){
        int32_t p=head[hash3(s,i)];
        if(p>=0&&i-size_t(p)<=4096){
          distance=i-size_t(p);
          while(length<18&&i+length<s.size()&&s[size_t(p)+length]==s[i+length])length++;
        }
      }
      size_t advance=1;
      if(length>=3){
        unsigned code=((distance-1)<<4)|(length-3);
        out.push_back(char(code));out.push_back(char(code>>8));advance=length;
      }else{flags|=1U<<bit;out.push_back(s[i]);}
      for(size_t j=0;j<advance;j++)if(i+j+2<s.size())head[hash3(s,i+j)]=int32_t(i+j);
      i+=advance;
    }
    out[flagpos]=char(flags);
  }
  return out;
}
inline bool header(const uint8_t *h,size_t &raw,size_t &stored,bool &compressed){
  if(h[0]!='W'||h[1]!='S'||(h[2]!='C'&&h[2]!='R')||h[3]!='1')return false;
  raw=get32(h+4);stored=get32(h+8);compressed=h[2]=='C';
  return raw<=MAX_RECORD&&stored<=MAX_RECORD&&(!compressed?stored==raw:stored<raw);
}
inline bool encode(const std::string &raw,std::string &frame){
  if(raw.size()>MAX_RECORD)return false;
  std::string packed=pack(raw);bool compressed=packed.size()<raw.size();
  frame=compressed?"WSC1":"WSR1";put32(frame,raw.size());put32(frame,compressed?packed.size():raw.size());put32(frame,crc((const uint8_t*)raw.data(),raw.size()));frame+=compressed?packed:raw;return true;
}
inline bool decode(const std::string &frame,std::string &out){
  out.clear();if(frame.size()<HEADER)return false;
  size_t raw,stored;bool compressed;
  auto h=(const uint8_t*)frame.data();
  if(!header(h,raw,stored,compressed)||frame.size()!=HEADER+stored)return false;
  out.reserve(raw);
  if(!compressed)out.assign(frame.data()+HEADER,stored);
  else{
    size_t p=HEADER;
    while(p<frame.size()&&out.size()<raw){
      uint8_t flags=frame[p++];
      for(unsigned bit=0;bit<8&&out.size()<raw;bit++){
        if(flags&(1U<<bit)){if(p>=frame.size())return false;out.push_back(frame[p++]);}
        else{
          if(p+2>frame.size())return false;
          unsigned code=uint8_t(frame[p])|(unsigned(uint8_t(frame[p+1]))<<8);p+=2;
          size_t distance=(code>>4)+1,length=(code&15)+3;
          if(distance>out.size()||out.size()+length>raw)return false;
          for(size_t k=0;k<length;k++)out.push_back(out[out.size()-distance]);
        }
      }
    }
    if(p!=frame.size())return false;
  }
  return out.size()==raw&&crc((const uint8_t*)out.data(),out.size())==get32(h+12);
}
// Shared by ESP32 and native tests. Reader(pos,buffer,size), emitter(string).
// Invalid byte spans are exported losslessly as quarantinable damage records.
template<class Reader,class Emitter>
bool exportJournal(size_t end,Reader read,Emitter emit){
  size_t pos=0,gap=0;uint8_t h[HEADER];
  auto damage=[&](size_t start,size_t stop)->bool {
    uint8_t bytes[256];const char *hex="0123456789abcdef";
    while(start<stop){
      size_t n=std::min(sizeof(bytes),stop-start);if(!read(start,bytes,n))return false;
      std::string line="{\"v\":2,\"type\":\"storage_damage\",\"offset\":"+std::to_string(start)+",\"raw_hex\":\"";
      for(size_t i=0;i<n;i++){line.push_back(hex[bytes[i]>>4]);line.push_back(hex[bytes[i]&15]);}
      line+="\"}\n";if(!emit(line))return false;start+=n;
    }
    return true;
  };
  while(pos<end){
    bool valid=false;std::string decoded;size_t raw=0,stored=0;bool compressed=false;
    if(end-pos>=HEADER){
      if(!read(pos,h,HEADER))return false;
      if(header(h,raw,stored,compressed)&&stored<=end-pos-HEADER){
        std::string frame((const char*)h,HEADER);frame.resize(HEADER+stored);
        if(stored&&!read(pos+HEADER,(uint8_t*)&frame[HEADER],stored))return false;
        valid=decode(frame,decoded);
      }
    }
    if(valid){
      if(gap<pos&&!damage(gap,pos))return false;
      if(!emit(decoded+"\n"))return false;
      pos+=HEADER+stored;gap=pos;
    }else pos++;
  }
  return gap==end||damage(gap,end);
}

}
