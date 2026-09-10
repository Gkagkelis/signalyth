"""Self-contained interactive dashboard for the client.

The PowerPoint is what you present; this is what the client explores afterwards.
It is a single HTML file with the data embedded, so it opens by double-click,
works offline, and can be emailed or hosted without any server, database or
licence.

Design rules that follow the methodology:

* every headline number is clickable and reveals the actual records behind it
  (§18 traceability: no number without a path back to evidence),
* filters never invent aggregates — the shown percentages are recomputed from the
  same records the report used, so the dashboard can never disagree with the deck,
* scope labels and limits travel with the data, not in a footnote nobody reads.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone


def _num(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _records_payload(records: list[dict]) -> list[dict]:
    out = []
    for r in records or []:
        ai = r.get("ai_analysis") or {}
        intel = r.get("intelligence") or {}
        sarcasm = ai.get("sarcasm")
        out.append({
            "id": str(r.get("id") or r.get("record_id") or ""),
            "platform": r.get("platform") or "",
            "author": r.get("author") or "",
            "date": str(r.get("date") or r.get("timestamp") or "")[:10],
            "url": r.get("url") or "",
            "text": str(r.get("text") or "")[:900],
            "sentiment": str(ai.get("sentiment_label") or ""),
            "score": _num(ai.get("sentiment_score"), 0.0),
            "stance": str(ai.get("target_stance") or ""),
            "emotion": str(ai.get("primary_emotion") or ""),
            "language": str(ai.get("language") or ""),
            "sarcasm": bool(sarcasm.get("detected")) if isinstance(sarcasm, dict) else bool(sarcasm),
            "origin": str(intel.get("origin_group") or ""),
            "impact": round(_num(intel.get("impact_score"), 0.0) or 0.0, 3),
            "topic": str(ai.get("topic") or ""),
            "narrative": str(ai.get("narrative") or ""),
            "human": bool((ai.get("human_review") or {}).get("verdict")),
        })
    return out


def build_dashboard_html(*, plan: dict, intelligence: dict, records: list[dict], language: str = "el") -> str:
    # Accept every form the pipeline uses: "el", "Ελληνικά", "greek", "gr".
    lang_norm = str(language or "").strip().lower()
    el = (lang_norm.startswith("el") or lang_norm.startswith("ελ")
          or lang_norm.startswith("gr") or lang_norm.startswith("greek"))
    ctx = {
        "client": plan.get("client", ""),
        "topic": plan.get("topic", ""),
        "market": plan.get("market", ""),
        "date_from": plan.get("date_from", ""),
        "date_to": plan.get("date_to", ""),
    }
    summary = intelligence or {}
    reputation = _num((summary.get("brand_reputation") or {}).get("index"))
    confidence = summary.get("confidence") or {}
    payload = {
        "ctx": ctx,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reputation": reputation,
        "confidence_label": confidence.get("label"),
        "confidence_score": _num(confidence.get("score")),
        "effective_voices": _num(summary.get("effective_independent_voices")),
        "records_total": summary.get("records"),
        "sentiment": (summary.get("sentiment") or {}).get("weighted_percent") or {},
        "records": _records_payload(records),
        "el": el,
    }
    # Record text is untrusted. Any "<" inside the embedded JSON is escaped, so no
    # markup from a post can ever terminate or alter the script block, and U+2028/29
    # cannot corrupt the parse. The data still decodes as ordinary JSON.
    data_json = (json.dumps(payload, ensure_ascii=False, allow_nan=False)
                 .replace("<", "\\u003c")
                 .replace("\u2028", "\\u2028")
                 .replace("\u2029", "\\u2029"))

    title = html.escape(f"{ctx['topic']} · {ctx['client']}".strip(" ·"))
    T = {
        "subtitle": "Διαδραστικό dashboard · κάθε αριθμός ανοίγει τα δεδομένα του" if el
                    else "Interactive dashboard · every number opens its own evidence",
        "reputation": "Brand Reputation" if not el else "Brand Reputation",
        "repscope": "Δείκτης ψηφιακής συζήτησης — όχι μέτρηση κοινής γνώμης" if el
                    else "Digital conversation signal — not a public-opinion measure",
        "confidence": "Βεβαιότητα evidence" if el else "Evidence confidence",
        "voices": "Ανεξάρτητες φωνές" if el else "Independent voices",
        "records": "Αναφορές" if el else "Records",
        "filters": "Φίλτρα" if el else "Filters",
        "all": "Όλα" if el else "All",
        "search": "Αναζήτηση στο κείμενο…" if el else "Search the text…",
        "showing": "Εμφανίζονται" if el else "Showing",
        "of": "από" if el else "of",
        "sentiment": "Sentiment",
        "platform": "Πηγή" if el else "Platform",
        "emotion": "Συναίσθημα" if el else "Emotion",
        "origin": "Προέλευση" if el else "Origin",
        "positive": "Θετικό" if el else "Positive",
        "negative": "Αρνητικό" if el else "Negative",
        "neutral": "Ουδέτερο" if el else "Neutral",
        "mixed": "Μικτό" if el else "Mixed",
        "empty": "Καμία αναφορά με αυτά τα φίλτρα." if el else "No records match these filters.",
        "impact": "Impact",
        "open": "Άνοιγμα" if el else "Open",
        "human": "ανθρώπινος έλεγχος" if el else "human reviewed",
        "note": ("Τα ποσοστά υπολογίζονται από τις ίδιες αναφορές που χρησιμοποιεί το report. "
                 "Ψηφιακά ίχνη, όχι δημοσκόπηση. Συσχέτιση ≠ αιτιότητα.") if el else
                ("Percentages are computed from the same records the report uses. "
                 "Digital traces, not a survey. Association is not causation."),
    }
    labels_json = json.dumps(T, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="{'el' if el else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SIGNALYTH · {title}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#F7F5F0;color:#1A1A1A;font:14px/1.55 Inter,-apple-system,Segoe UI,Roboto,Arial,sans-serif}}
.wrap{{max-width:1140px;margin:0 auto;padding:26px 18px 60px}}
.mark{{font-size:11px;letter-spacing:.22em;color:#5F6368;text-transform:uppercase}}
h1{{font-size:26px;margin:6px 0 2px}}
.sub{{color:#5F6368;font-size:13px;margin-bottom:20px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px}}
.kpi{{background:#fff;border:1px solid #E5E1D8;border-radius:14px;padding:14px 16px}}
.kpi span{{display:block;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:#5F6368;margin-bottom:6px}}
.kpi b{{font-size:28px;font-weight:700;color:#155E75}}
.kpi small{{display:block;color:#8A867E;font-size:10.5px;margin-top:4px}}
.panel{{background:#fff;border:1px solid #E5E1D8;border-radius:14px;padding:16px 18px;margin-bottom:16px}}
.bar{{display:flex;height:26px;border-radius:8px;overflow:hidden;margin:10px 0 6px;cursor:pointer}}
.bar div{{display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:700;transition:opacity .15s}}
.bar div:hover{{opacity:.85}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}}
.chip{{border:1px solid #DADADA;background:#fff;border-radius:999px;padding:5px 12px;font-size:12px;cursor:pointer}}
.chip.on{{background:#155E75;color:#fff;border-color:#155E75}}
input[type=search]{{width:100%;max-width:420px;padding:9px 12px;border:1px solid #DADADA;border-radius:10px;font-size:13px}}
.rec{{border-bottom:1px solid #EFEDE8;padding:12px 0}}
.rec:last-child{{border-bottom:0}}
.meta{{font-size:11px;color:#5F6368;margin-bottom:5px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.tag{{border-radius:999px;padding:2px 9px;font-size:10.5px;font-weight:700;color:#fff}}
.t-positive{{background:#1E7F4F}}.t-negative{{background:#B23B32}}.t-neutral{{background:#9AA0A6}}.t-mixed{{background:#C98A2B}}
.txt{{white-space:pre-wrap}}
a{{color:#155E75}}
.foot{{color:#8A867E;font-size:11px;margin-top:18px;line-height:1.6}}
.count{{font-size:12px;color:#5F6368;margin:10px 0}}
</style></head><body><div class="wrap">
<div class="mark">SIGNALYTH</div>
<h1>{title}</h1>
<div class="sub" id="sub"></div>
<div class="kpis" id="kpis"></div>
<div class="panel">
  <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#5F6368">Sentiment</div>
  <div class="bar" id="sentbar"></div>
  <div class="count" id="sentnote"></div>
</div>
<div class="panel">
  <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#5F6368;margin-bottom:6px" id="flabel"></div>
  <div id="filters"></div>
  <input type="search" id="q" style="margin-top:10px">
  <div class="count" id="count"></div>
</div>
<div class="panel" id="list"></div>
<div class="foot" id="foot"></div>
</div>
<script id="signalyth-data" type="application/json">{data_json}</script>
<script>
const D=JSON.parse(document.getElementById('signalyth-data').textContent);
const T={labels_json};
const S={{sentiment:null,platform:null,emotion:null,origin:null,q:''}};
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
const COL={{positive:'#1E7F4F',negative:'#B23B32',neutral:'#9AA0A6',mixed:'#C98A2B'}};
function filtered(){{
  return D.records.filter(r=>
    (!S.sentiment||r.sentiment===S.sentiment)&&
    (!S.platform||r.platform===S.platform)&&
    (!S.emotion||r.emotion===S.emotion)&&
    (!S.origin||r.origin===S.origin)&&
    (!S.q||((r.text+' '+r.author+' '+r.narrative).toLowerCase().includes(S.q))));
}}
function pct(rows,label){{ if(!rows.length)return 0; return 100*rows.filter(r=>r.sentiment===label).length/rows.length }}
function drawKpis(rows){{
  const k=document.getElementById('kpis');
  k.innerHTML=`
   <div class="kpi"><span>${{esc(T.reputation)}}</span><b>${{D.reputation!=null?D.reputation.toFixed(1):'—'}}</b><small>${{esc(T.repscope)}}</small></div>
   <div class="kpi"><span>${{esc(T.confidence)}}</span><b>${{esc(D.confidence_label||'—')}}</b><small>${{D.confidence_score!=null?D.confidence_score.toFixed(0)+'/100':''}}</small></div>
   <div class="kpi"><span>${{esc(T.voices)}}</span><b>${{D.effective_voices!=null?D.effective_voices.toFixed(0):'—'}}</b></div>
   <div class="kpi"><span>${{esc(T.records)}}</span><b>${{rows.length}}</b><small>${{esc(T.of)}} ${{D.records.length}}</small></div>`;
}}
function drawBar(rows){{
  const bar=document.getElementById('sentbar');
  const parts=['positive','negative','neutral','mixed'].map(l=>({{l,v:pct(rows,l)}})).filter(p=>p.v>0.4);
  bar.innerHTML=parts.map(p=>`<div style="width:${{p.v}}%;background:${{COL[p.l]}}" data-s="${{p.l}}" title="${{esc(T[p.l])}} ${{p.v.toFixed(1)}}%">${{p.v>7?p.v.toFixed(0)+'%':''}}</div>`).join('');
  bar.querySelectorAll('[data-s]').forEach(d=>d.onclick=()=>{{S.sentiment=S.sentiment===d.dataset.s?null:d.dataset.s;render()}});
  document.getElementById('sentnote').textContent=parts.map(p=>`${{T[p.l]}} ${{p.v.toFixed(1)}}%`).join(' · ');
}}
function chipRow(field,values){{
  return `<div class="chips">${{[''].concat(values).map(v=>`<button class="chip ${{(S[field]||'')===v?'on':''}}" data-f="${{field}}" data-v="${{esc(v)}}">${{v?esc(T[v]||v):esc(T.all)}}</button>`).join('')}}</div>`;
}}
function drawFilters(){{
  const uniq=f=>[...new Set(D.records.map(r=>r[f]).filter(Boolean))].sort();
  document.getElementById('flabel').textContent=T.filters;
  document.getElementById('filters').innerHTML=
    `<div style="font-size:11px;color:#5F6368">${{esc(T.sentiment)}}</div>`+chipRow('sentiment',['positive','negative','neutral','mixed'])+
    `<div style="font-size:11px;color:#5F6368">${{esc(T.platform)}}</div>`+chipRow('platform',uniq('platform'))+
    `<div style="font-size:11px;color:#5F6368">${{esc(T.emotion)}}</div>`+chipRow('emotion',uniq('emotion'))+
    `<div style="font-size:11px;color:#5F6368">${{esc(T.origin)}}</div>`+chipRow('origin',uniq('origin'));
  document.querySelectorAll('.chip').forEach(b=>b.onclick=()=>{{const f=b.dataset.f,v=b.dataset.v;S[f]=(S[f]===v||!v)?null:v;render()}});
}}
function drawList(rows){{
  const host=document.getElementById('list');
  if(!rows.length){{host.innerHTML=`<div class="count">${{esc(T.empty)}}</div>`;return}}
  const sorted=rows.slice().sort((a,b)=>b.impact-a.impact).slice(0,300);
  host.innerHTML=sorted.map(r=>`<div class="rec">
    <div class="meta">
      <span class="tag t-${{esc(r.sentiment||'neutral')}}">${{esc(T[r.sentiment]||r.sentiment||'—')}}</span>
      <span>${{esc(r.platform)}}</span><span>${{esc(r.author)}}</span><span>${{esc(r.date)}}</span>
      <span>${{esc(T.impact)}} ${{r.impact.toFixed(2)}}</span>
      ${{r.sarcasm?'<span>· sarcasm</span>':''}}${{r.human?`<span>· ${{esc(T.human)}}</span>`:''}}
      ${{r.url?`· <a href="${{esc(r.url)}}" target="_blank" rel="noopener">${{esc(T.open)}}</a>`:''}}
    </div><div class="txt">${{esc(r.text)}}</div></div>`).join('');
}}
function render(){{
  const rows=filtered();
  drawKpis(rows);drawBar(rows);drawList(rows);
  document.getElementById('count').textContent=`${{T.showing}} ${{rows.length}} ${{T.of}} ${{D.records.length}}`;
  document.querySelectorAll('.chip').forEach(b=>b.classList.toggle('on',(S[b.dataset.f]||'')===b.dataset.v));
}}
document.getElementById('sub').textContent=`${{T.subtitle}} · ${{D.ctx.market}} · ${{D.ctx.date_from}} → ${{D.ctx.date_to}}`;
document.getElementById('q').placeholder=T.search;
document.getElementById('q').oninput=e=>{{S.q=e.target.value.toLowerCase().trim();render()}};
document.getElementById('foot').textContent=T.note+' · '+new Date(D.generated_at).toLocaleString();
drawFilters();render();
</script></body></html>"""
