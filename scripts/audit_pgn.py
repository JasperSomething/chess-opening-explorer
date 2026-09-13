"""Fast header-only inventory. Does not claim to validate moves or deduplicate."""
import collections,datetime,hashlib,json,re,sys,time
from pathlib import Path
path=Path(sys.argv[1]);out=Path(sys.argv[2]);start=time.monotonic()
counts=collections.Counter();years=collections.Counter();controls=collections.Counter();samples=[]
headers={};in_headers=False
pattern=re.compile(rb'^\[([A-Za-z0-9_]+)\s+"(.*)"\]\s*$')

def emit(h):
    if not h:return
    counts['games']+=1
    if len(samples)<5:samples.append(h)
    def rating(key):
        try:return int(h.get(key,''))
        except ValueError:return 0
    white,black=rating('WhiteElo'),rating('BlackElo')
    if white and black:counts['both_ratings_present']+=1
    else:counts['one_or_both_ratings_missing']+=1
    for threshold in (2000,2100,2200,2300,2400,2500):
        if min(white,black)>=threshold:counts['both_at_least_'+str(threshold)]+=1
    counts['result_'+h.get('Result','missing')]+=1
    counts['variant_'+h.get('Variant','unspecified')]+=1
    if h.get('SetUp')=='1' or 'FEN' in h:counts['custom_start']+=1
    year=h.get('Date','????')[:4];years[year]+=1
    controls[h.get('TimeControl','missing')]+=1
    event=' '.join(h.get(x,'') for x in ('Event','Site','EventType')).lower()
    for label in ('blitz','rapid','online','bullet','corr'):
        if label in event:counts['event_or_site_mentions_'+label]+=1
    if counts['games']%250000==0:print(f"Headers scanned: {counts['games']:,}; {time.monotonic()-start:.0f}s",flush=True)

with path.open('rb') as f:
    for line in f:
        if line.startswith(b'['):
            m=pattern.match(line)
            if m:
                if m[1]==b'Event' and 'Event' in headers:
                    emit(headers);headers={}
                headers[m[1].decode('ascii')]=m[2].decode('utf-8',errors='replace')
                in_headers=True
        elif in_headers and line.strip():
            emit(headers);headers={};in_headers=False
    emit(headers)
report={'file':str(path.resolve()),'bytes':path.stat().st_size,'audited_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'seconds':time.monotonic()-start,'counts':dict(counts),'years':dict(sorted(years.items())),'time_controls_top_20':controls.most_common(20),'sample_headers':samples,'limitations':'Header inventory only. Rating filters use provided Elo tags. No independent duplicate detection or legal-move validation. Missing time controls cannot establish classical play.'}
out.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report['counts'],indent=2))
