"""FUSED Tender Intelligence - daily fetch, score and publish.

Pulls notices from Find a Tender and Contracts Finder (free, key-less OCDS APIs),
keeps electrical-compliance matches, scores them 0-100 and writes:
  site/index.html        the dashboard (published to Cloudflare Pages, behind Cloudflare Access)
  site/opportunities.csv CRM-ready export
  site/data.json         raw scored data

Usage:  pip install requests ; python fetch_and_score.py --days 45

Bid / No Bid: each card has a Bid / No Bid toggle. Decisions save to a shared Supabase table,
so everyone with the team passcode sees the same picture. One-off setup: run supabase_setup.sql
in Supabase, then set SUPABASE_URL and SUPABASE_ANON_KEY (repo secrets, or edit the constants below).
Without them the toggles still work but only save in each person's own browser.
"""
import argparse, csv, html, json, os, re, sys, time
from datetime import datetime, timedelta, timezone

import requests

FTS_BASE = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_BASE = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
PCS_BASE = "https://api.publiccontractsscotland.gov.uk/v1/Notices"
S2W_BASE = "https://api.sell2wales.gov.wales/v1/Notices"   # .gov.wales (the old .gov.uk address fails SSL)
# Shared Bid / No Bid store (Supabase). Both values are safe to publish: the table is locked
# down and every read/write needs the team passcode (see supabase_setup.sql).
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://xwyxgjxeorfuuueyqrte.supabase.co")  # Supabase project "Tenders"
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_eJpp7AcdfQ8BT8HIErSXEg_IEXUBBYx")  # public by design
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")  # GitHub secret - private; used only by the job
RENEWAL_MONTHS = 18          # renewals shown on the dashboard: contracts ending within this many months
UA = {"Accept": "application/json", "User-Agent": "FUSED-Tender-Intelligence/1.0"}

KW_HIGH = ["eicr", "electrical installation condition report", "electrical condition report",
           "fixed wire", "fixed wiring", "fixed electrical installation",
           "portable appliance", "pat testing", "pat test", "appliance testing",
           "emergency lighting", "emergency light"]
KW_MED = ["electrical compliance", "electrical safety", "electrical testing", "electrical inspection",
          "periodic inspection and testing", "periodic electrical", "statutory electrical",
          "electrical certification", "rcd testing", "bs 7671", "bs7671",
          "electrical remedial", "electrical statutory", "electrical maintenance"]
# Wider Fused services (remedials, installations, voltage optimisation / PFC, electrical maintenance)
KW_SERVICES = ["electrical installation work", "electrical installations", "electrical installation contract",
               "rewire", "rewiring", "electrical upgrade", "electrical works", "electrical services contract",
               "electrical term contract", "electrical contractor", "electrical repairs",
               "voltage optimisation", "voltage optimization", "voltage optimiser", "power factor correction",
               "power factor", "electrical maintenance", "electrical planned maintenance",
               "mechanical and electrical maintenance", "mechanical & electrical maintenance", "m&e maintenance",
               "m & e maintenance", "m&e services", "building services maintenance"]
# Broad phrases that only count when the notice is clearly electrical (the word "electric" appears)
KW_CONTEXT = ["remedial works", "remedials", "statutory compliance", "compliance testing", "statutory testing",
              "planned preventative maintenance", "ppm", "hard fm", "landlord compliance", "building compliance",
              "lightning protection", "thermographic", "thermal imaging survey"]
KW_LOW = []
EV_WORDS = ["ev charg", "electric vehicle charg", "charge point", "chargepoint", "charging point", "ev infrastructure"]
EV_ACTIONS = ["inspect", "testing", "periodic", "maintenance", "compliance", "certif"]
# CPV codes add confidence to a keyword match. Codes in CPV_TRIGGER can also find a notice on their own
# (e.g. a "Compliance framework" whose title never says "electrical"); those matches are capped at P3.
CPV_HIGH = {"71631000", "71630000"}
CPV_MED = {"50711000", "71314100", "45310000"}
CPV_TRIGGER = {
    "50711000": "repair and maintenance of electrical building installations",
    "71314100": "electrical services",
    "45310000": "electrical installation work",
    "45311000": "electrical wiring and fitting work",
    "45311100": "electrical wiring work",
    "45311200": "electrical fitting work",
    "45317000": "other electrical installation work",
    "71631100": "technical inspection services (machinery/installations)",
    "71632000": "technical testing services",
    "31518200": "emergency lighting equipment",
    "31527260": "lighting systems",
    "50532000": "repair and maintenance of electrical machinery",
    "71334000": "mechanical and electrical engineering services",
    "45315600": "low-voltage installation work",
    "31600000": "electrical equipment and apparatus",
}
EXCLUDE_TITLE = re.compile(r"\b(trainer|training|lease|back office|software|licen[cs]e|licensing|consultancy|calibration)\b")
WATCH = ["london and quadrant", "london & quadrant", "mid and south essex", "gloucestershire county",
         "rochdale boroughwide", "kirklees"]
KNOWN = ["085363-2026", "089933-2026", "089937-2026", "090449-2026", "091974-2026", "094380-2026",
         "ocds-h6vhtk-069fbf"]   # notices you told us about: always fetched directly and reported in Diagnostics
SECTOR = {"council": 15, "borough": 15, "county": 15, "city of": 15, "nhs": 15, "hospital": 15,
          "health board": 15, "housing": 14, "homes": 12, "trust": 12, "academy": 13, "school": 13,
          "college": 13, "university": 13, "fire and rescue": 12, "police": 10, "ministry of defence": 9}


def get(url, params, session, tries=4):
    """Fetch JSON with a few quick retries. Errors that won't fix themselves (SSL, 404) fail at once."""
    forbidden, last = 0, ""
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, headers=UA, timeout=60)
        except requests.exceptions.SSLError as e:      # certificate problem: retrying never helps
            raise RuntimeError(f"SSL error: {str(e)[:160]}")
        except requests.RequestException as e:         # timeouts / connection resets: short back-off
            last = f"{e.__class__.__name__}: {str(e)[:120]}"
            print(f"  network error ({last}), retry {attempt + 1}/{tries}", file=sys.stderr)
            time.sleep(10 * (attempt + 1)); continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                last = "response was not JSON"; time.sleep(5); continue
        if r.status_code == 403:      # Contracts Finder rate limit: it asks for a 5 minute wait
            forbidden += 1
            if forbidden > 3: break
            print("  rate limited (403), waiting 5 min", file=sys.stderr); time.sleep(300); continue
        if r.status_code in (429, 500, 502, 503, 504):  # Find a Tender allows 12 requests per 2 minutes
            wait = int(r.headers.get("Retry-After", 30)) + 10 * attempt
            last = f"HTTP {r.status_code}"
            if attempt == tries - 1:
                break
            print(f"  HTTP {r.status_code}, waiting {wait}s", file=sys.stderr); time.sleep(wait); continue
        r.raise_for_status()
    raise RuntimeError(f"gave up after {tries} tries ({last or 'rate limited'}): " + url[:120])


def next_link(data):
    links = data.get("links")
    if isinstance(links, dict):
        for k in ("next", "nextPage"):
            if links.get(k):
                return links[k]
    if isinstance(links, list):
        for l in links:
            if isinstance(l, dict) and l.get("rel") == "next" and l.get("href"):
                return l["href"]
    for k in ("next", "nextLink", "next_page"):
        if isinstance(data.get(k), str) and data[k]:
            return data[k]
    return None


def extract_releases(data):
    """PCS / Sell2Wales return notice lists in OCDS; accept a few possible shapes."""
    out = []
    if isinstance(data, list):
        for it in data: out.extend(extract_releases(it))
    elif isinstance(data, dict):
        if isinstance(data.get("releases"), list):
            out.extend(r for r in data["releases"] if isinstance(r, dict))
        elif "ocid" in data:
            out.append(data)
        else:
            for k in ("notices", "results", "items", "data", "packages"):
                if k in data:
                    out.extend(extract_releases(data[k]))
    return out


def month_list(n):
    d = datetime.now(timezone.utc).replace(day=1)
    out = []
    for _ in range(n):
        out.append(d.strftime("%m-%Y"))
        d = (d - timedelta(days=1)).replace(day=1)
    return out


def walk_feed(base, first_params, session, cutoff, max_pages=8000, deadline=None, info=None, pause=0.4, date_stop=True):
    """Walk the API's own paging links from newest to oldest until releases are older than cutoff."""
    url, params, pages = base, first_params, 0
    while pages < max_pages and (deadline is None or time.time() < deadline):
        data = get(url, params, session)
        rels = data.get("releases", [])
        pages += 1
        if info is not None:
            info["pages"] = info.get("pages", 0) + 1
            for r in rels:
                for tg in (r.get("tag") or ["?"]):
                    info.setdefault("tags", {})
                    info["tags"][tg] = info["tags"].get(tg, 0) + 1
                d = r.get("date", "")
                if d and (not info.get("oldest") or d < info["oldest"]):
                    info["oldest"] = d
        yield from rels
        nxt = next_link(data)
        if not nxt or not rels:
            if info is not None: info["stop"] = "end of feed"
            return
        dates = [r.get("date", "") for r in rels if r.get("date")]
        # Contracts Finder: stop once a whole page is older than the look-back. Find a Tender filters by
        # update date itself, and an old notice updated recently would stop the walk too early, so skip it there.
        if date_stop and dates and max(dates)[:10] < cutoff.strftime("%Y-%m-%d"):
            if info is not None: info["stop"] = "reached look-back date"
            return                       # whole page older than the look-back
        url, params = nxt, None
        time.sleep(pause)


def parse_dt(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def match(rel):
    """Does this notice concern Fused services? Returns (tender, title, keyword points, evidence) or None."""
    t = rel.get("tender") or {}
    title = t.get("title") or ""
    parts = [title, t.get("description") or "", rel.get("description") or ""]
    for lot in t.get("lots", []) or []:
        parts += [lot.get("title") or "", lot.get("description") or ""]
    for aw in rel.get("awards", []) or []:               # award notices describe the contract here
        parts += [aw.get("title") or "", aw.get("description") or ""]
    text = "\n".join(parts).lower()

    cpvs = []
    c = t.get("classification")
    if c and c.get("scheme") == "CPV":
        cpvs.append((c["id"], c.get("description", "")))
    for it in t.get("items", []) or []:
        for ac in [it.get("classification") or {}] + (it.get("additionalClassifications", []) or []):
            if ac.get("scheme") == "CPV" and ac.get("id"):
                cpvs.append((ac["id"], ac.get("description", "")))

    kw, ev = 0, []
    tl = title.lower()
    lot_titles = " | ".join((l.get("title") or "") for l in (t.get("lots") or [])).lower()
    body = "\n".join(parts[1:]).lower()[:40000]
    # skip notices that are clearly something else (training courses, leases, software) - unless the title
    # itself names one of our core services (e.g. "EICR and PAT testing incl. staff training" is kept)
    if EXCLUDE_TITLE.search(tl) and not any(k in tl for k in KW_HIGH):
        return None
    electrical = "electric" in text
    for lst, pts in ((KW_HIGH, 30), (KW_MED, 22), (KW_SERVICES, 20), (KW_CONTEXT if electrical else [], 16)):
        for k in lst:
            if k in tl:
                kw = max(kw, pts); ev.append(k + " (in title)")
            elif k in lot_titles:
                kw = max(kw, pts); ev.append(k + " (in lot title)")
            elif k in body:
                kw = max(kw, int(pts * 0.55)); ev.append(k + " (in description)")
    for blob, full in ((tl + " " + lot_titles, 26), (body, 15)):
        if any(w in blob for w in EV_WORDS) and any(w in blob for w in EV_ACTIONS):
            kw = max(kw, full); ev.append("EV charge point inspection/maintenance")
            break
    if kw == 0:
        trig = [(c, d) for c, d in cpvs if c in CPV_TRIGGER]
        if not trig:
            return None
        kw = 14                                   # classification-only match: shown, but never above P3
        ev += [f"CPV {c} {d or CPV_TRIGGER[c]} (classification only)" for c, d in trig]
    for code, d in cpvs:
        if code in CPV_HIGH or code in CPV_MED:
            ev.append(f"CPV {code} {d}"); kw = min(30, kw + 2)

    return t, title, kw, ev


def score(rel, portal, now):
    m = match(rel)
    if not m:
        return None
    t, title, kw, ev = m

    tags = rel.get("tag") or ["tender"]
    if "tender" in tags or "tenderUpdate" in tags:
        tag = "tender"
    elif "planning" in tags or "planningUpdate" in tags:
        tag = "planning"
    else:
        return None

    # deadline / staleness
    dl = parse_dt((t.get("tenderPeriod") or {}).get("endDate") or "")
    future = parse_dt((t.get("communication") or {}).get("futureNoticeDate") or "")
    stale, status, dl_pts = False, "", 8 if tag == "planning" else 4
    if tag == "tender" and dl:
        days = (dl - now).days
        if days < 0:
            return None                      # closed tender, drop
        dl_pts = 15 if days <= 60 else 8
        status = f"Live - closes in {days} days"
    elif tag == "tender":
        status = "Live - no deadline stated"
    else:
        status = "Pre-market / pipeline"
        if dl and dl < now: stale, status, dl_pts = True, "Pipeline - stated deadline has passed, verify", 2
        elif future and future < now: stale, status, dl_pts = True, "Pipeline - expected notice date has passed, verify", 2

    val = (t.get("value") or {}).get("amount")
    vpts = 8 if not val else 20 if val >= 1e6 else 16 if val >= 250e3 else 11 if val >= 50e3 else 6
    buyer = (rel.get("buyer") or {}).get("name", "")
    bl = buyer.lower()
    spts = next((p for k, p in SECTOR.items() if k in bl), 6)
    total = min(100, kw + vpts + (20 if tag == "planning" else 16) + spts + dl_pts)
    prio = "P1" if total >= 80 else "P2" if total >= 60 else "P3" if total >= 40 else "Watch"
    if stale and prio == "P1": prio = "P2"
    if not any("(in title)" in e or "(in lot title)" in e or e.startswith("EV charge") and kw >= 26 for e in ev) and prio in ("P1", "P2"):
        prio = "P3"

    if portal != "Find a Tender":
        url = next((d.get("url") for d in t.get("documents", []) or [] if d.get("documentType") in ("tenderNotice",) or "Notice" in (d.get("description") or "")), "")
        if not url:
            url = ((rel.get("planning") or {}).get("documents") or [{}])[0].get("url", "")
    else:
        url = f"https://www.find-tender.service.gov.uk/Notice/{rel.get('id')}"

    contact = ""
    for p in rel.get("parties", []) or []:
        if "buyer" in (p.get("roles") or []):
            cp = p.get("contactPoint") or {}
            contact = " / ".join(x for x in (cp.get("name"), cp.get("email"), cp.get("telephone")) if x)
            break

    return dict(priority=prio, fit_score=total, buyer=buyer, stage=tag, status=status, stale=stale,
                title=title, value_gbp=val, deadline=dl.date().isoformat() if dl else "",
                evidence="; ".join(dict.fromkeys(ev))[:300], contact=contact, portal=portal,
                url=url, ocid=rel.get("ocid"), released=rel.get("date", ""))


DECISION_JS = r"""
const CFG=__CFG__, shared=!!(CFG.url&&CFG.key);
let store={}, who='', pass='';
const LS={get:k=>{try{return localStorage.getItem(k)||''}catch(e){return ''}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
who=LS.get('fused-who'); pass=LS.get('fused-pass');
if(!shared){try{store=JSON.parse(LS.get('fused-bid-decisions')||'{}')}catch(e){}}
const cards=[...document.querySelectorAll('.card')];
let fShow='all',fDec='all';
const sync=document.getElementById('sync'), login=document.getElementById('login');
function status(t,bad){sync.textContent=t;sync.className=bad?'bad':'';}
function fmt(at){if(!at)return '';const d=new Date(at);return d.toLocaleDateString('en-GB',{day:'numeric',month:'short'})+' '+d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});}
function paint(){cards.forEach(c=>{const e=store[c.dataset.id];c.dataset.d=e?e.d:'';
  const rn=c.dataset.s==='renewal';
  c.querySelector('.who').textContent=e&&e.by?(e.d==='bid'?(rn?'Pursue':'Bid'):(rn?'Ignore':'No Bid'))+' by '+e.by+(e.at?', '+fmt(e.at):''):'';});apply();}
function apply(){const q=document.getElementById('q').value.toLowerCase();let b=0,n=0,r=0,v=0;
 cards.forEach(c=>{const d=c.dataset.d||'',rn=c.dataset.s==='renewal';
  if(!rn){if(d==='bid'){b++;v+=+c.dataset.v||0}else if(d==='nobid')n++;else r++;}
  const ok=(fShow==='all'?!rn:(c.dataset.p===fShow||c.dataset.s===fShow))&&(fDec==='all'||d===fDec)&&(!q||c.textContent.toLowerCase().includes(q));
  c.style.display=ok?'':'none';});
 sBid.textContent=b;sNo.textContent=n;sRev.textContent=r;sVal.textContent='£'+Math.round(v).toLocaleString('en-GB');}
async function rpc(fn,body){const h={'apikey':CFG.key,'Content-Type':'application/json'};
 if(CFG.key.startsWith('eyJ'))h['Authorization']='Bearer '+CFG.key;
 const r=await fetch(CFG.url.replace(/\/$/,'')+'/rest/v1/rpc/'+fn,{method:'POST',headers:h,body:JSON.stringify(body)});
 const t=await r.text();if(!r.ok)throw new Error(/passcode/i.test(t)?'passcode':'server '+r.status);return t?JSON.parse(t):null;}
async function load(){if(!shared){status('Saved in this browser only (shared storage not configured)');paint();return;}
 if(!pass||!who){login.style.display='flex';status('Sign in to see and record team decisions',true);paint();return;}
 try{const rows=await rpc('get_decisions',{p_pass:pass});store={};(rows||[]).forEach(x=>store[x.id]={d:x.decision,by:x.updated_by,at:x.updated_at});
  login.style.display='none';status('Shared with team · synced '+new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'})+' · '+who);paint();}
 catch(e){if(e.message==='passcode'){pass='';LS.set('fused-pass','');login.style.display='flex';status('Wrong passcode',true);}else status('Could not reach shared storage - retrying',true);}}
async function setDecision(c,v){const id=c.dataset.id,prev=store[id],nd=(c.dataset.d===v)?'':v;
 if(shared&&(!pass||!who)){login.style.display='flex';return;}
 if(nd)store[id]={d:nd,by:who||'',at:new Date().toISOString()};else delete store[id];paint();
 if(!shared){LS.set('fused-bid-decisions',JSON.stringify(store));return;}
 try{await rpc('set_decision',{p_pass:pass,p_id:id,p_decision:nd,p_by:who,p_title:c.querySelector('.title').textContent});status('Saved · '+who);}
 catch(e){if(prev)store[id]=prev;else delete store[id];paint();status('Not saved: '+(e.message==='passcode'?'wrong passcode':'could not reach shared storage'),true);}}
cards.forEach(c=>c.querySelectorAll('.db').forEach(btn=>btn.onclick=()=>setDecision(c,btn.dataset.set)));
document.getElementById('signin').onclick=()=>{who=document.getElementById('lwho').value.trim();pass=document.getElementById('lpass').value;
 if(!who||!pass)return;LS.set('fused-who',who);LS.set('fused-pass',pass);load();};
document.getElementById('signout').onclick=()=>{pass='';LS.set('fused-pass','');load();};
document.getElementById('q').oninput=apply;
document.querySelectorAll('.filters button[data-f]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.filters button[data-f]').forEach(x=>x.classList.remove('on'));b.classList.add('on');fShow=b.dataset.f;apply();});
document.querySelectorAll('#dfil button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#dfil button').forEach(x=>x.classList.remove('on'));b.classList.add('on');fDec=b.dataset.g;apply();});
function dl(name,text,type){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type}));a.download=name;a.click();}
expCsv.onclick=()=>{const esc=x=>'"'+String(x).replace(/"/g,'""')+'"';const rows=[['decision','decided_by','decided_at','priority','fit','title','buyer','deadline','value_gbp','url'].join(',')];
 cards.forEach(c=>{const e=store[c.dataset.id]||{};rows.push([e.d==='bid'?'Bid':e.d==='nobid'?'No Bid':'To review',e.by||'',e.at||'',c.dataset.p,(c.querySelector('.score')||{}).textContent||'',c.querySelector('.title').textContent,c.querySelector('.buyer').textContent.split(' · ')[0],(c.querySelector('.meta b')||{}).textContent||'',c.dataset.v,c.querySelector('.src a').href].map(esc).join(','))});
 dl('opportunities_with_decisions.csv',rows.join('\n'),'text/csv');};
load(); setInterval(load,30000); document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
"""


# ---------------------------------------------------------------- renewals (contracts ending soon)
def sb_headers(extra=None):
    h = {"apikey": SUPABASE_SERVICE_KEY, "Content-Type": "application/json"}
    if SUPABASE_SERVICE_KEY.startswith("eyJ"):
        h["Authorization"] = "Bearer " + SUPABASE_SERVICE_KEY
    h.update(extra or {})
    return h


def sb_upsert(table, rows, conflict="id"):
    for i in range(0, len(rows), 500):
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
                          headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          data=json.dumps(rows[i:i + 500], default=str), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {table} upsert failed: HTTP {r.status_code} {r.text[:200]}")


def sb_select(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers(), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase read failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def notice_url(rel, portal):
    if portal == "Find a Tender":
        return f"https://www.find-tender.service.gov.uk/Notice/{rel.get('id')}"
    t = rel.get("tender") or {}
    for d in (t.get("documents") or []) + (((rel.get("awards") or [{}])[0]).get("documents") or []):
        if d.get("url"):
            return d["url"]
    return ""


def renewals_from(rel, portal, now):
    """Turn an award notice for a Fused-type service into renewal rows (one per award with an end date)."""
    if not (rel.get("awards") or rel.get("contracts")):
        return []
    m = match(rel)
    if not m:
        return []
    t, title, kw, ev = m
    buyer = (rel.get("buyer") or {}).get("name", "")
    contracts = rel.get("contracts") or []
    awards = rel.get("awards") or [{}]
    contact = ""
    for p in rel.get("parties", []) or []:
        if "buyer" in (p.get("roles") or []):
            cp = p.get("contactPoint") or {}
            contact = " / ".join(x for x in (cp.get("name"), cp.get("email"), cp.get("telephone")) if x)
            break
    out = []
    for aw in awards:
        if (aw.get("status") or "") in ("cancelled", "unsuccessful"):
            continue
        ct = next((c for c in contracts if c.get("awardID") == aw.get("id")), contracts[0] if contracts else {})
        per = aw.get("contractPeriod") or ct.get("period") or t.get("contractPeriod") or {}
        start, end = parse_dt(per.get("startDate") or ""), parse_dt(per.get("endDate") or "")
        if not end and start and per.get("durationInDays"):
            end = start + timedelta(days=int(per["durationInDays"]))
        if not end or end < now - timedelta(days=30):
            continue
        val = ((aw.get("value") or {}).get("amount") or (ct.get("value") or {}).get("amount")
               or (t.get("value") or {}).get("amount"))
        sup = ", ".join(x.get("name", "") for x in (aw.get("suppliers") or []) if x.get("name"))
        rid = (rel.get("ocid") or "") + (f"/{aw.get('id')}" if len(awards) > 1 and aw.get("id") else "")
        out.append(dict(id=rid, ocid=rel.get("ocid"), title=(aw.get("title") or title)[:500], buyer=buyer[:300],
                        supplier=sup[:300], value_gbp=val,
                        contract_start=start.date().isoformat() if start else None,
                        contract_end=end.date().isoformat(),
                        awarded=(parse_dt(aw.get("date") or rel.get("date") or "") or now).date().isoformat(),
                        portal=portal, url=notice_url(rel, portal), evidence="; ".join(dict.fromkeys(ev))[:300],
                        contact=contact[:300], updated_at=now.isoformat()))
    return out


def scan_awards(session, frm, to, now, deadline=None, errors=None, infos=None):
    """Walk award notices published/updated between frm and to on Find a Tender and Contracts Finder."""
    found, counts = {}, {}
    f_s, t_s = frm.strftime("%Y-%m-%dT%H:%M:%S"), to.strftime("%Y-%m-%dT%H:%M:%S")
    feeds = [("Find a Tender awards", FTS_BASE,
              {"limit": 100, "stages": "award", "updatedFrom": f_s, "updatedTo": t_s}, "Find a Tender", 10.5),
             ("Contracts Finder awards", CF_BASE,
              {"publishedFrom": f_s, "publishedTo": t_s, "stages": "award", "limit": 100}, "Contracts Finder", 0.4)]
    for label, base, params, portal, pause in feeds:
        n, info = 0, (infos.setdefault(label, {}) if infos is not None else {})
        try:
            # keep the look-back stop here: Find a Tender sometimes ignores the month window on award notices
            for rel in walk_feed(base, params, session, frm, deadline=deadline, info=info, pause=pause):
                n += 1
                try:
                    for row in renewals_from(rel, portal, now):
                        found[row["id"]] = row
                except Exception as e:
                    if errors is not None and len(errors) < 40:
                        errors.append(f"renewal parse error on {rel.get('id')}: {e}")
        except Exception as e:
            if errors is not None:
                errors.append(f"{label} {f_s[:10]}..{t_s[:10]}: {str(e)[:200]}")
            print(f"  {label} FAILED: {e}", file=sys.stderr)
        counts[label] = n
        print(f"  {label} {f_s[:10]}..{t_s[:10]}: {n} award notices, {len(found)} renewals so far", file=sys.stderr)
    return list(found.values()), counts


def backfill_renewals(start=None, stop="2021-01"):
    """One-off history scan, newest month first. Saves after every month so a later run can resume."""
    if not SUPABASE_SERVICE_KEY:
        sys.exit("SUPABASE_SERVICE_KEY is not set - add it as a GitHub secret first.")
    now = datetime.now(timezone.utc)
    resume = not start          # only the automatic backfill moves the saved resume point
    if not start:
        st = sb_select("app_state?key=eq.backfill_next&select=value")
        start = st[0]["value"] if st else now.strftime("%Y-%m")
    if start == "done":
        print("Backfill already complete.", file=sys.stderr)
        return
    deadline = time.time() + 5 * 3600
    s = requests.Session()
    y, mth = map(int, start.split("-"))
    total = 0
    while f"{y:04d}-{mth:02d}" >= stop:
        if time.time() > deadline - 20 * 60:
            print(f"Time limit reached - next run resumes at {y:04d}-{mth:02d}", file=sys.stderr)
            break
        frm = datetime(y, mth, 1, tzinfo=timezone.utc)
        to = datetime(y + (mth == 12), mth % 12 + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        rows, _ = scan_awards(s, frm, min(to, now), now, deadline=deadline)
        if rows:
            sb_upsert("renewals", rows)
        total += len(rows)
        y, mth = (y - 1, 12) if mth == 1 else (y, mth - 1)
        nxt = f"{y:04d}-{mth:02d}" if f"{y:04d}-{mth:02d}" >= stop else "done"
        if resume:
            sb_upsert("app_state", [dict(key="backfill_next", value=nxt, updated_at=now.isoformat())], conflict="key")
        print(f"Month {frm:%Y-%m} done: {len(rows)} renewals saved (total {total}). Next: {nxt}", file=sys.stderr)
    print(f"Backfill finished this run: {total} renewals saved.", file=sys.stderr)


def render(rows, stats, updated, diag=None, renewals=None):
    E = html.escape
    def money(v): return f"£{v:,.0f}" if v else "not stated"
    cards = []
    for r in rows:
        stale = '<span class="stale">VERIFY STATUS</span>' if r["stale"] else ""
        cards.append(f"""<div class="card" data-p="{r['priority']}" data-s="{r['stage']}" data-id="{E(str(r['ocid'] or r['url']))}" data-d="" data-v="{r['value_gbp'] or 0}">
<div class="top"><div><span class="badge {r['priority']}">{r['priority']}</span><span class="score">Fit {r['fit_score']}/100</span>{stale}</div>
<div class="meta">Deadline: <b>{E(r['deadline'] or 'n/a')}</b> &middot; Value: <b>{money(r['value_gbp'])}</b></div></div>
<div class="title">{E(r['title'])}</div>
<div class="buyer">{E(r['buyer'])} &middot; {E(r['status'])} &middot; {E(r['portal'])}</div>
<div class="ev"><b>Evidence:</b> {E(r['evidence'])}</div>
{f'<div class="ev"><b>Buyer contact:</b> {E(r["contact"])}</div>' if r['contact'] else ''}
<div class="bottom"><div class="src"><a href="{E(r['url'])}" target="_blank" rel="noopener">Open source notice &#8599;</a></div>
<div class="decide"><span class="who"></span><span class="dl">Decision:</span><button class="db bid" data-set="bid">Bid</button><button class="db nobid" data-set="nobid">No Bid</button></div></div></div>""")
    today = datetime.now(timezone.utc).date()
    for r in renewals or []:
        try:
            end = datetime.fromisoformat(str(r["contract_end"])[:10]).date()
        except Exception:
            continue
        months = max(0, round((end - today).days / 30.4))
        win, wcls = (("Prime window", "prime") if 6 <= months <= 12 else
                     ("Ending soon", "soon") if months < 6 else ("Early sight", "early"))
        contact = f'<div class="ev"><b>Buyer contact:</b> {E(r["contact"])}</div>' if r.get("contact") else ""
        cards.append(f"""<div class="card renewal" data-p="R" data-s="renewal" data-id="renewal:{E(str(r['id']))}" data-d="" data-v="{r.get('value_gbp') or 0}">
<div class="top"><div><span class="badge R">RENEWAL</span><span class="win {wcls}">{win}</span></div>
<div class="meta">Contract ends: <b>{end:%d %b %Y}</b> (in {months} months) &middot; Value: <b>{money(r.get('value_gbp'))}</b></div></div>
<div class="title">{E(r.get('title') or '')}</div>
<div class="buyer">{E(r.get('buyer') or '')} &middot; Current supplier: {E(r.get('supplier') or 'not stated')} &middot; Awarded {E(str(r.get('awarded') or ''))} &middot; {E(r.get('portal') or '')}</div>
<div class="ev"><b>Evidence:</b> {E(r.get('evidence') or '')}</div>
{contact}<div class="bottom"><div class="src"><a href="{E(r.get('url') or '')}" target="_blank" rel="noopener">Open award notice &#8599;</a></div>
<div class="decide"><span class="who"></span><span class="dl">Decision:</span><button class="db bid" data-set="bid">Pursue</button><button class="db nobid" data-set="nobid">Ignore</button></div></div></div>""")
    body = "\n".join(cards) or '<p>No matching opportunities in this window.</p>'
    diag_html = ""
    if diag:
        cnt = "".join(f"<li>{E(k)}: {v} releases scanned</li>" for k, v in diag["counts"].items())
        wl = "".join(f"<li><b>{E(w['buyer'])}</b> - {E(w['title'])} [{E(w['stage'])}, {E(w['date'][:10])}] "
                     f"<a href=\"{E(w['url'])}\" target=\"_blank\">notice</a></li>" for w in diag["watch"][:80]) or "<li>None of the watched buyers appeared in the feeds.</li>"
        err_html = "".join(f"<li>{E(x)}</li>" for x in diag.get("errors", [])) or "<li>none</li>"
        info_html = "".join(f"<li>{E(k)}: {v.get('pages', 0)} pages, stopped: {E(str(v.get('stop', 'time limit or error')))}, oldest release {E(str(v.get('oldest', ''))[:10])}, tags {E(str(v.get('tags', {})))}</li>" for k, v in diag["infos"].items())
        known_html = "".join(f"<li>{E(k['id'])} - {E(k['title'])} [{E(k['tags'])}] - {E(k['result'])}</li>" for k in diag["known"]) or "<li>none</li>"
        diag_html = (f"<details style='margin-top:24px'><summary><b>Diagnostics</b> (for checking coverage)</summary>"
                     f"<p>Failed feeds: {diag['failed']}</p><ul>{cnt}</ul>"
                     f"<p><b>Errors:</b></p><ul>{err_html}</ul>"
                     f"<p><b>Feed details:</b></p><ul>{info_html}</ul>"
                     f"<p><b>Notices you named, fetched directly:</b></p><ul>{known_html}</ul>"
                     f"<p><b>Watched buyers - every notice found, matched or not:</b></p><ul>{wl}</ul></details>")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FUSED Tender Intelligence</title><style>
:root{{--navy:#0f2540;--accent:#e8622c;--bg:#f4f6f8;--b:#dfe4ea;--m:#5c6b7a}}
*{{box-sizing:border-box}}body{{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:#1c2733}}
header{{background:linear-gradient(135deg,#0f2540,#16324f);color:#fff;padding:26px 32px}}header h1{{margin:0 0 6px;font-size:22px}}header p{{margin:0;color:#cdd8e3;font-size:14px}}
.wrap{{max-width:1200px;margin:0 auto;padding:22px 32px 60px}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
.stat{{background:#fff;border:1px solid var(--b);border-radius:10px;padding:12px 16px;min-width:120px}}.stat .n{{font-size:22px;font-weight:700;color:var(--navy)}}.stat .l{{font-size:11px;color:var(--m);text-transform:uppercase}}
.filters{{margin-bottom:16px}}.filters button{{border:1px solid var(--b);background:#fff;border-radius:20px;padding:6px 14px;margin-right:6px;cursor:pointer;font-size:13px}}.filters button.on{{background:var(--navy);color:#fff}}
.card{{background:#fff;border:1px solid var(--b);border-radius:10px;padding:16px 18px;margin-bottom:12px}}.top{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
.badge{{color:#fff;font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px}}.P1{{background:#c0392b}}.P2{{background:#d98c1f}}.P3{{background:#2d7dd2}}.Watch{{background:#7f8c9a}}
.score,.meta{{font-size:13px;color:var(--m);margin-left:8px}}.meta{{margin:0}}.stale{{margin-left:8px;font-size:11px;font-weight:700;color:#a13a1a;background:#fdecdf;border-radius:20px;padding:2px 8px}}
.title{{font-size:16px;font-weight:700;color:var(--navy);margin:8px 0 2px}}.buyer{{font-size:13.5px;color:var(--m);margin-bottom:8px}}.ev{{font-size:13.5px;margin-top:5px}}
.src a{{color:var(--accent);font-weight:600;text-decoration:none;font-size:13px}}
.bottom{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid var(--b)}}
.decide{{display:flex;align-items:center;gap:6px}}.dl{{font-size:12px;color:var(--m);text-transform:uppercase;margin-right:2px}}
.db{{border:1px solid var(--b);background:#fff;border-radius:20px;padding:5px 14px;cursor:pointer;font-size:13px;font-weight:600;color:var(--m)}}
.card[data-d=bid] .db.bid{{background:#1e8449;border-color:#1e8449;color:#fff}}.card[data-d=nobid] .db.nobid{{background:#7f8c9a;border-color:#7f8c9a;color:#fff}}
.card[data-d=bid]{{border-left:4px solid #1e8449}}.card[data-d=nobid]{{border-left:4px solid #b8c1cb;opacity:.7}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;background:#fff;border:1px solid var(--b);border-radius:10px;padding:12px 16px;align-items:center}}
.summary .it{{min-width:110px}}.summary .n{{font-size:20px;font-weight:700}}.summary .l{{font-size:11px;color:var(--m);text-transform:uppercase}}
.summary .bidc{{color:#1e8449}}.summary .nobidc{{color:#7f8c9a}}.summary .revc{{color:#d98c1f}}.summary .act{{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}}
.summary .act button,.summary .act label{{border:1px solid var(--b);background:#fff;border-radius:20px;padding:6px 12px;cursor:pointer;font-size:12.5px}}
.who{{font-size:12px;color:var(--m);margin-right:6px}}#sync{{font-size:12px;color:#1e8449;align-self:center}}#sync.bad{{color:#a13a1a}}
.login{{display:none;gap:8px;align-items:center;flex-wrap:wrap;background:#fff8f1;border:1px solid #f3c9a8;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px}}
.login input{{padding:6px 12px;border:1px solid var(--b);border-radius:20px;font-size:13px}}.login button{{background:var(--navy);color:#fff;border:0;border-radius:20px;padding:7px 16px;cursor:pointer}}.login span{{color:var(--m);font-size:12px}}
.R{{background:#6c3fb5}}.win{{margin-left:8px;font-size:11px;font-weight:700;border-radius:20px;padding:2px 8px}}
.win.prime{{color:#1e6b3a;background:#dff3e6}}.win.soon{{color:#a13a1a;background:#fdecdf}}.win.early{{color:#5c6b7a;background:#eef1f4}}
.frow{{margin-bottom:10px}}.frow .fl{{font-size:12px;color:var(--m);text-transform:uppercase;margin-right:8px}}.foot{{font-size:12.5px;color:var(--m);margin-top:30px;line-height:1.6}}</style></head><body>
<header><h1>FUSED Tender Intelligence</h1><p>PAT, EICR, fixed wire, emergency lighting, EV inspection, remedials, installations, voltage optimisation and electrical maintenance opportunities from Find a Tender, Contracts Finder, Public Contracts Scotland and Sell2Wales. Updated daily. Last refresh: {updated}.</p></header>
<div class="wrap"><div class="stats">
<div class="stat"><div class="n">{len(rows)}</div><div class="l">Opportunities</div></div>
<div class="stat"><div class="n">{stats['P1']}</div><div class="l">P1</div></div>
<div class="stat"><div class="n">{stats['P2']}</div><div class="l">P2</div></div>
<div class="stat"><div class="n">{stats['tender']}</div><div class="l">Live tenders</div></div>
<div class="stat"><div class="n">{stats['planning']}</div><div class="l">Pre-market</div></div>
<div class="stat"><div class="n">{len(renewals or [])}</div><div class="l">Renewals ({RENEWAL_MONTHS} mo)</div></div>
<div class="stat"><div class="n">{stats['scanned']}</div><div class="l">Notices scanned</div></div></div>
<div class="summary"><div class="it"><div class="n bidc" id="sBid">0</div><div class="l">Bid</div></div>
<div class="it"><div class="n nobidc" id="sNo">0</div><div class="l">No Bid</div></div>
<div class="it"><div class="n revc" id="sRev">0</div><div class="l">To review</div></div>
<div class="it"><div class="n bidc" id="sVal">&pound;0</div><div class="l">Bid pipeline value</div></div>
<div class="act"><span id="sync"></span><button id="signout" title="Forget the passcode on this device">Sign out</button><button id="expCsv">CSV with decisions</button></div></div>
<div id="login" class="login"><b>Team sign-in</b><input id="lwho" placeholder="Your name (e.g. Maaz)"><input id="lpass" type="password" placeholder="Team passcode"><button id="signin">Sign in</button><span>Decisions are shared with everyone who has the passcode.</span></div>
<div class="filters"><div class="frow"><span class="fl">Show</span><input id="q" placeholder="Search buyer or title" style="padding:6px 12px;border:1px solid #dfe4ea;border-radius:20px;margin-right:10px;font-size:13px;width:220px"><button class="on" data-f="all">All</button><button data-f="P1">P1</button><button data-f="P2">P2</button><button data-f="tender">Live tenders</button><button data-f="planning">Pre-market</button><button data-f="renewal">Renewals</button> <a href="opportunities.csv" style="font-size:13px;margin-left:10px">Download CSV</a></div>
<div class="frow" id="dfil"><span class="fl">Decision</span><button class="on" data-g="all">All</button><button data-g="bid">Bid</button><button data-g="nobid">No Bid</button><button data-g="">To review</button></div></div>
<div id="cards">{body}</div>
{diag_html}<div class="foot">Score (0-100) = service match (30) + value (20) + stage, pre-market scores higher (20) + buyer sector fit (15) + deadline urgency (15). Both source APIs filter only by stage and date, so every notice is scanned and matched locally. Always check the source notice before acting.</div></div>
<script>{DECISION_JS.replace("__CFG__", json.dumps(dict(url=SUPABASE_URL, key=SUPABASE_KEY)))}</script>
</body></html>"""


def run(days, outdir):
    days = max(days, 150)   # open tenders can be months old; closed ones are dropped locally
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    s = requests.Session()
    best, seen_ids, scanned, ok = {}, set(), 0, 0
    counts, failed, watch, infos, known = {}, 0, [], {}, []
    deadline = time.time() + 150 * 60   # stop walking feeds after 2.5 hours so the run always finishes and publishes

    def consider(rel, portal):
        nonlocal scanned
        rid = (rel.get("ocid"), rel.get("id"))
        if rid in seen_ids: return None
        seen_ids.add(rid); scanned += 1
        bname = ((rel.get("buyer") or {}).get("name") or "")
        if any(w in bname.lower() for w in WATCH):
            wt = rel.get("tender") or {}
            wurl = (f"https://www.find-tender.service.gov.uk/Notice/{rel.get('id')}" if portal == "Find a Tender"
                    else next((d.get("url", "") for d in wt.get("documents", []) or []), ""))
            watch.append(dict(buyer=bname, title=wt.get("title", ""), stage="/".join(rel.get("tag") or []),
                              date=rel.get("date", ""), url=wurl))
        try:
            r = score(rel, portal, now)
        except Exception as e:
            r = None
            if len(errors) < 25: errors.append(f"score error on {rel.get('id')}: {e}")
        wtxt = (bname + " " + ((rel.get("tender") or {}).get("title") or "")).lower()
        if any(w in wtxt for w in ("swindon", "ca18576")) and not any(x.get("title") == (rel.get("tender") or {}).get("title") for x in watch):
            wt = rel.get("tender") or {}
            watch.append(dict(buyer=bname, title=wt.get("title", "") + (" [MATCHED]" if r else " [NOT MATCHED]"), stage="/".join(rel.get("tag") or []),
                              date=rel.get("date", ""), url=next((d.get("url", "") for d in wt.get("documents", []) or []), "")))
        if r:
            key = r["ocid"]
            rank = (r["stage"] == "tender", r["released"])
            if key not in best or rank > best[key][0]:
                best[key] = (rank, r)
        return r

    errors = []
    cf_common = {"publishedFrom": cutoff.strftime("%Y-%m-%dT00:00:00"),
                 "publishedTo": now.strftime("%Y-%m-%dT%H:%M:%S"), "limit": 100}
    fts_from = (now - timedelta(days=min(days, 120))).strftime("%Y-%m-%dT00:00:00")
    fts_to = now.strftime("%Y-%m-%dT%H:%M:%S")
    feeds = [("Find a Tender (tender)", FTS_BASE, {"limit": 100, "stages": "tender", "updatedFrom": fts_from, "updatedTo": fts_to},
              "Find a Tender"),
             ("Find a Tender (planning)", FTS_BASE, {"limit": 100, "stages": "planning", "updatedFrom": fts_from, "updatedTo": fts_to},
              "Find a Tender"),
             ("Contracts Finder (tender)", CF_BASE, dict(cf_common, stages="tender"), "Contracts Finder"),
             ("Contracts Finder (planning)", CF_BASE, dict(cf_common, stages="planning"), "Contracts Finder")]
    for label, base, params, portal in feeds:
        print(f"{label}: walking feed", file=sys.stderr)
        info = infos.setdefault(label, {})
        n0 = scanned
        try:
            fts = portal == "Find a Tender"
            pause = 10.5 if fts else 0.4   # Find a Tender: max 12 requests per 2 minutes
            for rel in walk_feed(base, params, s, cutoff, deadline=deadline, info=info, pause=pause, date_stop=not fts):
                consider(rel, portal)
            ok += 1
        except Exception as e:
            failed += 1
            errors.append(f"{label}: {e}")
            print(f"  {label} FAILED: {e}", file=sys.stderr)
        counts[label] = scanned - n0
        print(f"  {scanned - n0} releases", file=sys.stderr)

    # Scotland and Wales portals (below-threshold notices are published only there)
    for label, base, portal in (("Public Contracts Scotland", PCS_BASE, "Public Contracts Scotland"),
                                ("Sell2Wales", S2W_BASE, "Sell2Wales")):
        n0 = scanned
        info = infos.setdefault(label, {"pages": 0})
        fails = 0
        for nt in (2, 102, 1, 101):          # contract notice, site contract notice, prior info, site prior info
            for mm in month_list(6):
                if fails >= 2:               # portal is down or unreachable: skip the rest rather than waste hours
                    break
                try:
                    q = {"dateFrom": mm, "noticeType": nt, "outputType": 0}
                    if portal == "Sell2Wales": q["locale"] = 2057     # English
                    data = get(base, q, s, tries=1)
                    rels = extract_releases(data)
                    info["pages"] += 1; fails = 0
                    for rel in rels:
                        consider(rel, portal)
                except Exception as e:
                    if "HTTP 5" not in str(e):   # their server bug on one request: skip it, keep going
                        fails += 1
                    errors.append(f"{label} type {nt} month {mm}: {str(e)[:200]}")
                    print(f"  {label} type {nt} month {mm} failed: {str(e)[:200]}", file=sys.stderr)
                time.sleep(0.5)
        if fails >= 2:
            failed += 1
            errors.append(f"{label}: skipped after 2 failures in a row")
        counts[label] = scanned - n0
        print(f"  {label}: {scanned - n0} releases", file=sys.stderr)

    # notices you named: fetch directly so they can never be missed by the feed walk
    for nid in KNOWN:
        try:
            time.sleep(10.5)                 # stay inside Find a Tender's rate limit
            data = get(f"{FTS_BASE}/{nid}", None, s, tries=2)
            rels = data.get("releases", [])
            for rel in rels:
                consider(rel, "Find a Tender")
            t0 = (rels[-1].get("tender") or {}) if rels else {}
            matched = any(rel.get("ocid") in best for rel in rels)
            known.append(dict(id=nid, title=t0.get("title", "(no title)"), tags="/".join(rels[0].get("tag") or []) if rels else "",
                              result="matched - shown above" if matched else "fetched but not matched/closed by filters"))
        except Exception as e:
            known.append(dict(id=nid, title="", tags="", result=f"could not fetch: {str(e)[:80]}"))
    # renewals: add award notices from the last 3 days, then read everything ending in the next RENEWAL_MONTHS
    renewals = []
    if SUPABASE_SERVICE_KEY:
        try:
            new, rc = scan_awards(s, now - timedelta(days=3), now, now, deadline=time.time() + 30 * 60,
                                  errors=errors, infos=infos)
            counts.update(rc)
            if new:
                sb_upsert("renewals", new)
            horizon = (now + timedelta(days=int(RENEWAL_MONTHS * 30.4))).date().isoformat()
            renewals = sb_select(f"renewals?select=*&contract_end=gte.{now.date().isoformat()}"
                                 f"&contract_end=lte.{horizon}&order=contract_end.asc&limit=3000")
            print(f"Renewals: {len(new)} new/updated, {len(renewals)} ending within {RENEWAL_MONTHS} months", file=sys.stderr)
        except Exception as e:
            errors.append(f"Renewals: {str(e)[:200]}")
            print(f"  Renewals FAILED: {e}", file=sys.stderr)
    else:
        errors.append("Renewals off: add the SUPABASE_SERVICE_KEY secret in GitHub")
    rows = [v[1] for v in best.values()]
    if ok == 0 or scanned == 0:
        sys.exit("No data retrieved - leaving previous site untouched.")
    rows.sort(key=lambda r: (r["stale"], -r["fit_score"]))
    stats = dict(P1=sum(r["priority"] == "P1" for r in rows), P2=sum(r["priority"] == "P2" for r in rows),
                 tender=sum(r["stage"] == "tender" for r in rows), planning=sum(r["stage"] == "planning" for r in rows),
                 scanned=scanned)
    os.makedirs(outdir, exist_ok=True)
    updated = now.strftime("%d %b %Y %H:%M UTC")
    open(os.path.join(outdir, "index.html"), "w", encoding="utf-8").write(render(rows, stats, updated, dict(counts=counts, failed=failed, errors=errors, infos=infos, known=known, watch=sorted(watch, key=lambda w: w["date"], reverse=True)), renewals))
    json.dump(dict(updated=updated, stats=stats, rows=rows), open(os.path.join(outdir, "data.json"), "w"), indent=1)
    with open(os.path.join(outdir, "opportunities.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["priority"])
        w.writeheader(); w.writerows(rows)
    with open(os.path.join(outdir, "renewals.csv"), "w", newline="", encoding="utf-8") as fh:
        cols = ["contract_end", "buyer", "title", "supplier", "value_gbp", "contract_start", "awarded", "portal", "url", "contact", "evidence"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(renewals)
    print(f"Done: {len(rows)} matches from {scanned} notices, {len(renewals)} renewals -> {outdir}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--out", default="site")
    ap.add_argument("--backfill-renewals", action="store_true", help="one-off scan of past award notices into Supabase")
    ap.add_argument("--start", help="backfill: newest month to scan, YYYY-MM (default: resume where the last run stopped)")
    ap.add_argument("--stop", default="2021-01", help="backfill: oldest month to scan, YYYY-MM")
    a = ap.parse_args()
    if a.backfill_renewals:
        backfill_renewals(a.start, a.stop)
    else:
        run(a.days, a.out)