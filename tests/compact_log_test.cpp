#include "../firmware/Locus/CompactLog.h"
#include <cassert>
#include <fstream>
#include <iostream>
#include <random>
#include <iterator>
using namespace CompactLog;
std::string exportBytes(const std::string &data){
  std::string out;
  bool ok=exportJournal(data.size(),[&](size_t p,uint8_t *b,size_t n){if(p+n>data.size())return false;std::copy(data.begin()+p,data.begin()+p+n,b);return true;},[&](const std::string &s){out+=s;return true;});
  assert(ok);return out;
}
void roundtrip(const std::string &s){std::string frame,out;assert(encode(s,frame));assert(decode(frame,out));assert(s==out);}
int main(int argc,char **argv){
  std::mt19937 rng(20261029);
  roundtrip("");roundtrip("x");roundtrip(std::string(65536,'a'));
  std::string frame;assert(!encode(std::string(65537,'a'),frame));
  for(int i=0;i<1000;i++){
    size_t n=rng()%5000;std::string sample;for(size_t j=0;j<n;j++)sample.push_back(char(rng()%((i%2)?256:5)));roundtrip(sample);
  }
  std::string a,b;encode("{\"record_id\":1}",a);encode("{\"record_id\":2}",b);
  assert(exportBytes(a+b)=="{\"record_id\":1}\n{\"record_id\":2}\n");
  // Every truncation of a first frame must preserve the following valid frame.
  for(size_t i=1;i<a.size();i++){
    auto out=exportBytes(a.substr(0,i)+b);
    assert(out.find("storage_damage")!=std::string::npos);
    assert(out.find("{\"record_id\":2}\n")!=std::string::npos);
  }
  // Every single-byte corruption must be caught, with subsequent data recoverable.
  for(size_t i=0;i<a.size();i++){
    auto bad=a;bad[i]^=0x80;std::string ignored;assert(!decode(bad,ignored));
    auto out=exportBytes(bad+b);assert(out.find("storage_damage")!=std::string::npos);assert(out.find("{\"record_id\":2}\n")!=std::string::npos);
  }
  // Invalid backreferences, oversized lengths, and read/write failures.
  auto bad=a;bad[4]=bad[5]=bad[6]=bad[7]=char(255);std::string ignored;assert(!decode(bad,ignored));
  assert(!exportJournal(a.size(),[](size_t,uint8_t*,size_t){return false;},[](const std::string&){return true;}));
  assert(!exportJournal(a.size(),[&](size_t p,uint8_t *x,size_t n){std::copy(a.begin()+p,a.begin()+p+n,x);return true;},[](const std::string&){return false;}));
  if(argc==4){
    std::ifstream in(argv[1]);std::string line,stored,expected;
    while(std::getline(in,line)){assert(encode(line,frame));stored+=frame;expected+=line+"\n";}
    auto out=exportBytes(stored);assert(out==expected);
    std::ofstream(argv[2],std::ios::binary).write(stored.data(),stored.size());
    std::ofstream(argv[3],std::ios::binary).write(out.data(),out.size());
    std::cout<<"Original JSONL: "<<expected.size()<<" bytes; compact flash: "<<stored.size()<<" bytes; ratio: "<<double(expected.size())/stored.size()<<"x\n";
  }
  std::cout<<"PASS: 1003 codec roundtrips, size bounds, every frame-byte corruption/truncation, recovery and I/O failures\n";
}
