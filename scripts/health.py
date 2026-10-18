#!/usr/bin/env python3
"""Health dashboard of one website system (website-platform-kit decision 0008), modelled on the agentic platform kit's
per-system health dashboard: the system's structure (repositories, environments UAT then prod, workflows, domain) and
its health (every node probed, last deploys, CI on main, scorecard highlights), on one static page.

Reads only public data: this system's repositories through the GitHub API (GH_TOKEN: the run's own token), its UAT and
prod addresses over HTTPS, and its scorecard-data branch. Keeps a rolling probe history (7 days) by reading the
previously published history.json from its own Pages address, so nothing is committed. Writes --out: index.html,
health.json (everything the page shows) and history.json.
"""
import argparse, datetime as dt, html, json, os, time, urllib.error, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
UTC = dt.timezone.utc
KEEP_DAYS = 7


def get(url, token=None, raw=False, timeout=20):
    h = {'User-Agent': 'site-health-dashboard'}
    if token and url.startswith('https://api.github.com/'):
        h |= {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            data = r.read()
            return data.decode() if raw else json.loads(data)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def probe(url):
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={'User-Agent': 'site-health-dashboard', 'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(200000).decode(errors='replace')
            code = r.status
    except urllib.error.HTTPError as e:
        code, body = e.code, ''
    except (urllib.error.URLError, TimeoutError) as e:
        return {'code': None, 'ms': None, 'error': str(getattr(e, 'reason', e))[:120], 'noindex': None}
    return {'code': code, 'ms': round((time.monotonic() - t0) * 1000), 'noindex': 'name="robots" content="noindex' in body}


def runs(repo, workflow, token, n=20, **q):
    qs = '&'.join(f'{k}={v}' for k, v in q.items())
    d = get(f'https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page={n}&{qs}', token) or {}
    return [{'id': r['id'], 'url': r['html_url'], 'event': r['event'], 'branch': r['head_branch'], 'sha': r['head_sha'][:7],
             'title': r.get('display_title', ''), 'status': r['status'], 'conclusion': r['conclusion'],
             'at': r.get('run_started_at') or r['created_at'], 'updated': r['updated_at']} for r in d.get('workflow_runs', [])]


def collect(cfg, token, previous):
    now = dt.datetime.now(UTC)
    repo = cfg['repository']
    data = {'generated': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'system': {k: cfg.get(k) for k in ('slug', 'name', 'owner', 'domain', 'framework')},
            'repositories': [], 'environments': [], 'workflows': [], 'missing': []}
    for r in [repo, cfg.get('uatRepository')]:
        if not r:
            continue
        m = get(f'https://api.github.com/repos/{r}', token)
        if not m:
            data['missing'].append(r)
            continue
        data['repositories'].append({'name': r, 'url': m['html_url'], 'role': 'site' if r == repo else 'UAT host',
                                     'visibility': m['visibility'], 'defaultBranch': m['default_branch'], 'pushed': m['pushed_at']})
    pages = get(f'https://api.github.com/repos/{repo}/pages', token) or {}
    data['domain'] = {'name': cfg.get('domain'), 'connected': bool(pages.get('cname')), 'cname': pages.get('cname'),
                      'https': pages.get('https_enforced')}
    ci = runs(repo, cfg.get('ciWorkflow', 'ci.yml'), token, 10, branch='main')
    done = [r for r in ci if r['status'] == 'completed']
    data['green'] = {'state': (done[0]['conclusion'] if done else None), 'run': done[0] if done else None, 'inProgress': any(r['status'] != 'completed' for r in ci)}
    hist = previous.get('samples', []) if previous else []
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    hist = [s for s in hist if dt.datetime.fromisoformat(s['t'].replace('Z', '+00:00')) >= cutoff]
    for env in cfg['environments']:
        rs = runs(repo, env['workflow'], token, 30, **({'branch': env['branch']} if env.get('branch') else {}))
        fin = [r for r in rs if r['status'] == 'completed' and r['conclusion'] not in ('skipped', 'cancelled')]
        week = [r for r in fin if dt.datetime.fromisoformat(r['at'].replace('Z', '+00:00')) >= now - dt.timedelta(days=7)]
        p = probe(env['url'])
        sample = {'t': data['generated'], 'env': env['name'], 'code': p['code'], 'ms': p['ms']}
        hist.append(sample)
        mine = [s for s in hist if s['env'] == env['name']]
        up = [s for s in mine if s['code'] and 200 <= s['code'] < 400]
        node_ok = bool(p['code'] and 200 <= p['code'] < 400)
        last = fin[0] if fin else None
        state = 'healthy' if node_ok and (not last or last['conclusion'] == 'success') else ('down' if not node_ok else 'degraded')
        data['environments'].append({
            'name': env['name'], 'label': env.get('label', env['name']), 'url': env['url'], 'host': env.get('host', ''),
            'workflow': env['workflow'], 'state': state, 'probe': p,
            'uptime': {'samples': len(mine), 'up': len(up), 'share': round(len(up) / len(mine), 4) if mine else None,
                       'medianMs': sorted(s['ms'] for s in mine if s['ms'] is not None)[len([s for s in mine if s['ms'] is not None]) // 2] if any(s['ms'] is not None for s in mine) else None,
                       'recent': [{'t': s['t'], 'code': s['code']} for s in mine[-48:]]},
            'lastDeploy': last, 'inFlight': next((r for r in rs if r['status'] != 'completed'), None),
            'deploys7d': {'count': len(week), 'failed': sum(1 for r in week if r['conclusion'] != 'success')},
            'recent': fin[:5]})
    for w in cfg.get('workflows', []):
        rs = runs(repo, w['file'], token, 1)
        data['workflows'].append({'file': w['file'], 'name': w.get('name', w['file']), 'purpose': w.get('purpose', ''),
                                  'url': f'https://github.com/{repo}/actions/workflows/{w["file"]}', 'last': rs[0] if rs else None})
    raw = f'https://raw.githubusercontent.com/{repo}/scorecard-data'
    latest = get(f'{raw}/latest.json')
    if latest:
        v = latest.get('views', {})
        data['scorecard'] = {'today': latest.get('today'), 'rolling7': v.get('rolling7'), 'rolling30': v.get('rolling30'),
                             'rolling12Weeks': v.get('rolling12Weeks'),
                             'report': f'https://github.com/{repo}/blob/scorecard-data/index.html',
                             'reportPreview': f'https://htmlpreview.github.io/?{raw}/index.html',
                             'data': f'https://github.com/{repo}/tree/scorecard-data'}
    else:
        data['missing'].append('scorecard-data/latest.json')
    events = []
    for env in data['environments']:
        events += [dict(r, kind=f'{env["label"]} deploy') for r in env['recent']]
    events += [dict(r, kind='CI') for r in done[:5]]
    data['events'] = sorted(events, key=lambda e: e['at'], reverse=True)[:12]
    data['platformDashboard'] = cfg.get('platformDashboard')
    data['nodes'] = {'total': len(data['environments']), 'healthy': sum(1 for e in data['environments'] if e['state'] == 'healthy')}
    return data, {'samples': hist}


CSS = """
:root{--bg:#0f1b2d;--bar:#0b1524;--side:#122036;--card:#16263f;--line:#2a3d5c;--ink:#e6edf6;--muted:#93a4bd;--link:#7cc4ff;
--ok:#1f9d55;--ok-fill:#16794a;--warn:#e0a400;--crit:#e5484d;--crit-fill:#a52a2e;--cm-blue:#004B87;--cm-primary:#0085CA;--cm-sky:#24ABE1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Roboto,"Segoe UI",system-ui,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.top{display:flex;align-items:center;gap:16px;background:var(--bar);border-bottom:1px solid var(--line);padding:10px 20px}
.brand{font-weight:600;letter-spacing:.02em}.pill{border:1px solid var(--line);border-radius:4px;padding:3px 10px;color:var(--muted)}
main{max-width:1280px;margin:0 auto;padding:20px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px}.card .v{font-size:24px;font-weight:600}.card .k{color:var(--muted);font-size:12px}
.card.bad{border-color:var(--crit)}.card.good{border-color:var(--ok)}
.env{background:var(--card);border:1px solid var(--line);border-radius:6px;margin:12px 0;padding:14px 16px}
.env h3{margin:0 0 8px;font-size:15px;display:flex;gap:10px;align-items:center}
.state{font-size:12px;border-radius:10px;padding:1px 9px;font-weight:600}.state.healthy,.state.success{background:var(--ok-fill)}
.state.down,.state.failure{background:var(--crit-fill)}.state.degraded,.state.attention{background:#8a6500}.state.none{background:#33475f}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
.box{border:1px solid var(--line);border-radius:6px;padding:10px 12px;background:#132339}.box .t{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.spark{display:flex;gap:2px;margin-top:6px}.spark i{display:block;width:6px;height:16px;border-radius:1px;background:var(--ok)}.spark i.x{background:var(--crit)}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;font-size:12px}
.tile{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:3px;font-size:12px;font-weight:700;margin-right:6px;vertical-align:-3px}
.tile.success{background:var(--ok-fill)}.tile.failure{background:var(--crit-fill)}.tile.run{background:var(--cm-sky)}.tile.none{background:#33475f}
.flow{display:flex;flex-wrap:wrap;align-items:stretch;gap:8px}.node{background:var(--cm-blue);border:1px solid var(--cm-primary);border-radius:6px;padding:8px 12px;min-width:150px}
.arrow{align-self:center;color:var(--muted)}footer{color:var(--muted);font-size:12px;margin:30px 0 10px}
"""


def esc(v):
    return html.escape('' if v is None else str(v))


def ago(ts, now):
    if not ts:
        return 'never'
    s = (now - dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))).total_seconds()
    return f'{int(s // 60)} min ago' if s < 3600 else (f'{s / 3600:.1f} h ago' if s < 172800 else f'{s / 86400:.0f} days ago')


def et(ts):
    return dt.datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(ET).strftime('%b %d %H:%M ET') if ts else '–'


def tile(r):
    if not r:
        return '<span class="tile none">·</span>'
    if r['status'] != 'completed':
        return '<span class="tile run">↻</span>'
    return '<span class="tile success">✓</span>' if r['conclusion'] == 'success' else '<span class="tile failure">✕</span>'


def run_line(r, now):
    if not r:
        return '<span class="muted">no run</span>'
    return f'{tile(r)}<a href="{esc(r["url"])}">{esc(r["sha"])}</a> · {esc(r["conclusion"] or r["status"])} · {esc(et(r["at"]))} <span class="muted">({esc(ago(r["at"], now))})</span>'


def num(v, d=0):
    return '–' if v is None else (f'{v:.{d}f}' if d else str(v))


def render(d):
    now = dt.datetime.fromisoformat(d['generated'].replace('Z', '+00:00'))
    s = d['system']
    n = d['nodes']
    title = f'{n["healthy"]} of {n["total"]} nodes healthy – {s["name"]}'
    green = d['green']
    sc = d.get('scorecard') or {}
    r7 = sc.get('rolling7') or {}
    r30 = sc.get('rolling30') or {}
    today = sc.get('today') or {}
    dep = lambda a, e: ((a or {}).get('deployments') or {}).get(e) or {}
    prod7 = dep(r7, 'prod')
    cards = [(f'{n["healthy"]}/{n["total"]}', 'nodes healthy', 'good' if n['healthy'] == n['total'] else 'bad'),
             ('green' if green['state'] == 'success' else (green['state'] or 'unknown'), 'main (CI)', 'good' if green['state'] == 'success' else 'bad')]
    for e in d['environments']:
        u = e['uptime']['share']
        cards.append((f'{u * 100:.1f}%' if u is not None else '–', f'{e["label"]} uptime ({e["uptime"]["samples"]} probes, 7 d)', 'good' if u == 1 else ('bad' if u is not None and u < .99 else '')))
    cards += [(num((today.get('counts') or {}).get('itemsDelivered')), 'delivered today', ''),
              (num(next((e['deploys7d']['count'] for e in d['environments'] if e['name'] == 'prod'), None)), 'prod deploys, last 7 days', ''),
              (num((r30.get('delivery') or {}).get('mttdDays'), 3), 'lead time days (MTTD, 30 d)', '')]
    h = [f'<header class="top"><span class="brand">palermo-sites</span><span class="pill">Health dashboard</span>'
         + (f'<a class="pill" id="to-platform" href="{esc(d["platformDashboard"])}">Platform dashboard</a>' if d.get('platformDashboard') else '') + '</header><main>',
         f'<h1>{esc(s["name"])}</h1><p class="muted">{esc(title)} · read <span data-ago="{esc(d["generated"])}">{esc(et(d["generated"]))}</span> · owner {esc(s.get("owner"))}</p>',
         '<div class="cards">' + ''.join(f'<div class="card {c}"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>' for v, k, c in cards) + '</div>']
    # structure
    repos = {r['role']: r for r in d['repositories']}
    site, uatr = repos.get('site'), repos.get('UAT host')
    h.append('<h2>Structure</h2><div class="flow">')
    h.append(f'<div class="node"><b>Pull request</b><br><span class="muted">branch → review</span></div><span class="arrow">→</span>')
    for i, e in enumerate(d['environments']):
        h.append(f'<div class="node"><b>{esc(e["label"])}</b><br><a href="{esc(e["url"])}">{esc(e["url"].split("//")[1])}</a><br><span class="muted">{esc(e["host"])} · {esc(e["workflow"])}</span></div>')
        if i < len(d['environments']) - 1:
            h.append('<span class="arrow">→ merge →</span>')
    h.append('</div><div class="grid" style="margin-top:10px">')
    if site:
        h.append(f'<div class="box"><div class="t">Site repository</div><a href="{esc(site["url"])}">{esc(site["name"])}</a><br><span class="muted">{esc(site["visibility"])} · default branch {esc(site["defaultBranch"])} · pushed {esc(ago(site["pushed"], now))}</span></div>')
    if uatr:
        h.append(f'<div class="box"><div class="t">UAT repository</div><a href="{esc(uatr["url"])}">{esc(uatr["name"])}</a><br><span class="muted">GitHub Pages from {esc(uatr["defaultBranch"])}, written only by the UAT workflow</span></div>')
    dm = d['domain']
    h.append(f'<div class="box"><div class="t">Domain</div>{esc(dm["name"] or "none")} <span class="state {"healthy" if dm["connected"] else "none"}">{"connected" if dm["connected"] else "not connected"}</span><br><span class="muted">{"CNAME " + esc(dm["cname"]) if dm["cname"] else "served at the GitHub Pages address until DNS points here"}</span></div>')
    h.append(f'<div class="box"><div class="t">Main is green</div>{run_line(green["run"], now)}{" <span class=muted>(a run is in progress)</span>" if green["inProgress"] else ""}</div></div>')
    h.append('<table style="margin-top:10px"><thead><tr><th>Workflow</th><th>Purpose</th><th>Last run</th></tr></thead><tbody>'
             + ''.join(f'<tr><td><a href="{esc(w["url"])}">{esc(w["name"])}</a> <span class="muted">{esc(w["file"])}</span></td><td>{esc(w["purpose"])}</td><td>{run_line(w["last"], now)}</td></tr>' for w in d['workflows'])
             + '</tbody></table>')
    # environments
    h.append('<h2>Environments</h2>')
    for e in d['environments']:
        p, u = e['probe'], e['uptime']
        spark = ''.join(f'<i class="{"" if x["code"] and 200 <= x["code"] < 400 else "x"}" title="{esc(et(x["t"]))}: {esc(x["code"])}"></i>' for x in u['recent'])
        h.append(f'<section class="env"><h3>{esc(e["label"])} <span class="state {esc(e["state"])}">{esc(e["state"])}</span> <a class="muted" href="{esc(e["url"])}">{esc(e["url"])}</a></h3><div class="grid">'
                 f'<div class="box"><div class="t">Node</div>HTTP <b>{esc(p["code"] or p.get("error"))}</b> in {esc(num(p["ms"]))} ms'
                 + (f'<br><span class="muted">noindex {"yes" if p["noindex"] else "no"}</span>' if p.get('noindex') is not None else '') + '</div>'
                 f'<div class="box"><div class="t">Uptime, 7 days</div><b>{esc(f"{u["share"] * 100:.2f}%" if u["share"] is not None else "–")}</b> of {u["samples"]} probes · median {esc(num(u["medianMs"]))} ms<div class="spark">{spark}</div></div>'
                 f'<div class="box"><div class="t">Last deploy</div>{run_line(e["lastDeploy"], now)}'
                 + (f'<br>{tile(e["inFlight"])}deploying {esc(e["inFlight"]["sha"])} now' if e['inFlight'] else '') + '</div>'
                 f'<div class="box"><div class="t">Deploys, last 7 days</div><b>{e["deploys7d"]["count"]}</b>, failed {e["deploys7d"]["failed"]}'
                 f'<br><span class="muted">change failure rate {esc(f"{e["deploys7d"]["failed"] / e["deploys7d"]["count"] * 100:.0f}%" if e["deploys7d"]["count"] else "–")}</span></div></div></section>')
    # delivery
    if sc:
        def row(label, a, days):
            a = a or {}
            c = a.get('counts') or {}
            p_, u_ = dep(a, 'prod'), dep(a, 'uat')
            fr = (p_.get('count', 0) / days) if days else None
            return (f'<tr><td>{esc(label)}</td><td>{num(c.get("itemsStarted"))}</td><td>{num(c.get("itemsDelivered"))}</td><td>{num(c.get("wipEndOfDay"))}</td>'
                    f'<td>{num((a.get("delivery") or {}).get("mttdDays"), 3)}</td><td>{num(u_.get("count"))}</td><td>{num(p_.get("count"))}</td>'
                    f'<td>{num(fr, 2) if days else "–"}</td><td>{esc(f"{p_["failed"] / p_["count"] * 100:.0f}%" if p_.get("count") else "–")}</td></tr>')
        h.append('<h2>Delivery (scorecard)</h2><table><thead><tr><th>Window</th><th>Started</th><th>Delivered</th><th>WIP</th><th>Lead time days (MTTD)</th>'
                 '<th>UAT deploys</th><th>Prod deploys</th><th>Deploys per day</th><th>Change failure rate</th></tr></thead><tbody>'
                 + row(f'Today so far ({today.get("date", "")})', today, None) + row('Last 7 days', r7, 7) + row('Last 30 days', r30, 30)
                 + row('Rolling 12 weeks', sc.get('rolling12Weeks'), None) + '</tbody></table>'
                 f'<p class="muted">Daily facts from the scorecard-data branch (America/New_York days). <a href="{esc(sc["reportPreview"])}">Scorecard report</a> · <a href="{esc(sc["data"])}">data</a></p>')
    h.append('<h2>What happened</h2><table><thead><tr><th>When</th><th>What</th><th>Commit</th><th>Outcome</th></tr></thead><tbody>'
             + ''.join(f'<tr><td>{esc(et(e["at"]))}</td><td>{esc(e["kind"])} · {esc(e["title"][:80])}</td><td><a href="{esc(e["url"])}">{esc(e["sha"])}</a></td><td>{tile(e)}{esc(e["conclusion"])}</td></tr>' for e in d['events'])
             + '</tbody></table>')
    if d['missing']:
        h.append(f'<p class="muted">Not read: {esc(", ".join(d["missing"]))}</p>')
    h.append(f'<footer>Written by workflow <code>health</code> every 30 minutes from public GitHub data and HTTPS probes · <a href="health.json">health.json</a> · <a href="history.json">history.json</a> · kit decision 0008</footer></main>')
    js = "document.querySelectorAll('[data-ago]').forEach(e=>{const m=Math.round((Date.now()-new Date(e.dataset.ago))/60000);e.textContent=m<1?'just now':m+' min ago'});"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{esc(title)}</title><style>{CSS}</style></head><body>{"".join(h)}<script>{js}{JS_FRAME}</script></body></html>\n')


JS_FRAME = "// Shown inside the platform dashboard's zoom (kit decision 0009): Platform dashboard zooms back out there.\n(()=>{const a=document.getElementById('to-platform');if(!a||window.top===window)return;let same=false;try{same=window.parent.location.origin===location.origin}catch(e){}if(!same)return;a.textContent='\u2190 Platform';a.addEventListener('click',e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();window.parent.postMessage('platform-zoom-out',location.origin)})})();"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(Path(__file__).resolve().parent.parent / 'health.config.json'))
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    token = os.environ.get('GH_TOKEN')
    previous = get(cfg['pagesUrl'].rstrip('/') + '/history.json?' + str(int(time.time()))) if cfg.get('pagesUrl') else None
    data, history = collect(cfg, token, previous)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'health.json').write_text(json.dumps(data, indent=2) + '\n')
    (out / 'history.json').write_text(json.dumps(history) + '\n')
    (out / 'index.html').write_text(render(data))
    (out / '.nojekyll').write_text('')
    print(f'{data["nodes"]["healthy"]} of {data["nodes"]["total"]} nodes healthy; main {data["green"]["state"]}; missing: {data["missing"] or "nothing"}')


if __name__ == '__main__':
    main()
