"""Generate claude-usage-by-work-item.json.

Usage: python3 gen_dashboard.py claude-usage-by-work-item.json
Edit the panels here, not the JSON. Also writes <out>.queries.json for validating
every PromQL expression against the data source (delete it afterwards).
"""
import json, os, sys
DS = {"type": "prometheus", "uid": "${ds}"}
# Latest work-item tag per session. Unticketed (WORKLOG) sessions show as
# "WORKLOG: <work log entry title>" so each entry is its own row.
_SEL = ('{"__name__"="log_work.session.info", work_item=~`${work_item:regex}`, '
        '"user.email"=~`${email:regex}`, work_item%s"WORKLOG"}')
_KEY = 'label_replace(last_over_time(%s[90d]), "work_key", "$1", "work_item", "(.*)")'  # raw key, for links
JIRA_RE = "[A-Z][A-Z0-9]+-[0-9]+"
INFO = ('(0 * label_replace(topk by ("session.id") (1, '
        f'label_replace({_KEY % (_SEL % "=")}, "work_item", "WORKLOG: $1", "work_title", "(.+)")'
        f' or {_KEY % (_SEL % "!=")}), "jira", "$1", "work_key", "({JIRA_RE})") + 1)')
# Usage the log-work hook derives from transcripts (all session types, incl. desktop).
# Native export reaches Google from desktop only with the shell-profile workaround, so
# metrics both sources have use the larger per-session value (see best()).
HOOK = {"cost.usage": "log_work.usage.cost_usd", "token.usage": "log_work.usage.tokens",
        "lines_of_code.count": "log_work.usage.lines", "api_seconds": "log_work.usage.api_seconds",
        "tool_seconds": "log_work.usage.tool_seconds", "commit.count": "log_work.usage.commits",
        "pull_request.count": "log_work.usage.prs", "turns": "log_work.usage.turns",
        "tool_calls": "log_work.usage.tool_calls"}
MODELED = {"cost.usage", "token.usage"}  # carry a model label; filtered by the Model variable
def _used_model():
    """Sessions that used a model matching the Model variable (any session when All)."""
    return " or ".join(f'count by ("session.id") (last_over_time({M("cost.usage", native=n)}[90d]))' for n in (False, True))
BOTH = {"cost.usage", "token.usage", "lines_of_code.count", "commit.count", "pull_request.count"}
def M(name, extra="", native=False):
    if name in MODELED:
        extra = ", ".join(x for x in (extra, 'model=~`${model:regex}`') if x)
    if name in HOOK and not native:
        return "{" + f'"__name__"="{HOOK[name]}"' + (f", {extra}" if extra else "") + "}"
    sel = f'"__name__"="claude_code.{name}"' + (f", {extra}" if extra else "")
    return "{" + sel + ', job=~"claude-code|claude-code-desktop"}'
def best(q, name, extra):
    """q(selector) per session, from the hook and native export, keeping the larger.
    Native is live but covers only the current process; the hook covers every run
    but lags until the process stops."""
    # ponytail: undercounts a resumed session while it runs; exact once it stops
    if name not in BOTH:
        return q(M(name, extra))
    n, h = q(M(name, extra, native=True)), q(M(name, extra))
    return f"(({n} > {h}) or {h} or {n})"
def per_session(name, extra="", by=""):
    b = '"session.id"' + (f", {by}" if by else "")
    # latest value, not max: totals are cumulative per session, and a corrected
    # (lower) resend must replace the earlier one
    return best(lambda sel: f"sum by ({b}) (last_over_time({sel}[$__range]))", name, extra)
INFO = f'({INFO} and on("session.id") ({_used_model()}))'  # Model filter applies to every panel
def joined(inner, by="work_item", labels="work_item, work_title"):
    return f"sum by ({by}) ({inner} * on(\"session.id\") group_left({labels}) {INFO})"
def rate_joined(name, extra="", by="work_item", inner_by=""):
    b = '"session.id"' + (f", {inner_by}" if inner_by else "")
    inner = best(lambda sel: f"sum by ({b}) (increase({sel}[$__interval]))", name, extra)
    return f"sum by ({by}) ({inner} * on(\"session.id\") group_left(work_item) {INFO})"

def tgt(expr, ref, legend="", instant=True, fmt="time_series"):
    t = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend, "range": not instant, "instant": instant}
    if fmt == "table": t["format"] = "table"
    return t

USER, CLI, ADDED = 'type="user"', 'type="cli"', 'type="added"'
CACHE, INPUTS = 'type="cacheRead"', 'type=~"input|cacheRead|cacheCreation"'
panels, y = [], 0
def add(p, w, h, x=None):
    global y
    p.setdefault("datasource", DS)
    p["gridPos"] = {"x": add.x, "y": y, "w": w, "h": h}
    add.x += w
    if add.x >= 24: add.x, y = 0, y + h
    p["id"] = len(panels) + 1
    panels.append(p)
add.x = 0
def row(title):
    global y
    if add.x: add.x, y = 0, y + 8
    panels.append({"type": "row", "title": title, "collapsed": False, "id": len(panels) + 1,
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
    y += 1

def stat(title, expr, unit="none", decimals=None):
    p = {"type": "stat", "title": title, "targets": [tgt(expr, "A")],
         "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
         "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none", "graphMode": "none"}}
    if decimals is not None: p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p

row("Overview")
for title, expr, unit, dec in [
    ("Cost (est.)", f"sum({joined(per_session('cost.usage'))})", "currencyUSD", 2),
    ("Active hours (you, CLI only)", f"sum({joined(per_session('active_time.total', USER))}) / 3600", "none", 1),
    ("Claude hours (model + tools)", f"(sum({joined(per_session('api_seconds'))}) + sum({joined(per_session('tool_seconds'))})) / 3600", "none", 1),
    ("Sessions", f"sum(count by (work_item) ({per_session('cost.usage')} * on(\"session.id\") group_left(work_item) {INFO}))", "none", 0),
    ("Lines added", f"sum({joined(per_session('lines_of_code.count', ADDED))})", "none", 0),
    ("Commits", f"sum({joined(per_session('commit.count'))})", "none", 0),
    ("Turns (your prompts)", f"sum({joined(per_session('turns'))})", "none", 0),
    ("Tool calls", f"sum({joined(per_session('tool_calls'))})", "none", 0),
]:
    add(stat(title, expr, unit, dec), 3, 4)
add({"type": "stat", "title": "Unassigned cost (no work item)", "targets": [tgt(
    f"sum({per_session('cost.usage')} unless on(\"session.id\") last_over_time({{\"__name__\"=\"log_work.session.info\"}}[90d]))", "A")],
    "fieldConfig": {"defaults": {"unit": "currencyUSD", "decimals": 2}, "overrides": []},
    "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none", "graphMode": "none"},
    "description": "Sessions with usage but no log_work.session.info tag (log-work not tracking, or tag failed)."}, 24, 3)

row("By work item")
cols = [("A", "Cost", per_session("cost.usage")),
        ("B", "Tokens", per_session("token.usage")),
        ("C", "Active h (you)", per_session("active_time.total", 'type="user"'), " / 3600"),
        ("D", "Claude h (model)", per_session("api_seconds"), " / 3600"),
        ("E", "Lines added", per_session("lines_of_code.count", 'type="added"')),
        ("F", "Commits", per_session("commit.count")),
        ("G", "PRs", per_session("pull_request.count")),
        ("I", "Turns", per_session("turns")),
        ("J", "Tool calls", per_session("tool_calls"))]
KEYS = 'work_item, work_title, work_key, jira, "user.email"'
targets = [tgt(joined(c[2], by=KEYS, labels=KEYS) + (c[3] if len(c) > 3 else ""), c[0], fmt="table") for c in cols]
SESSIONS = f'count by ({KEYS}) ({per_session("cost.usage")} * on("session.id") group_left({KEYS}) {INFO})'
targets.append(tgt(SESSIONS, "H", fmt="table"))
for ref, name in [("K", "commit.count"), ("L", "pull_request.count")]:  # outcomes per session
    targets.append(tgt(f"{joined(per_session(name), by=KEYS, labels=KEYS)} / {SESSIONS}", ref, fmt="table"))
# Clicking a work item filters this dashboard to it; ticket keys also get a Jira link.
FILTER_URL = ("/d/claude-usage-work-item/claude-usage-by-work-item?var-work_item=${__data.fields.work_key}"
              "&${ds:queryparam}&${email:queryparam}&${model:queryparam}&${__url_time_range}")
USER_URL = ("/d/claude-usage-work-item/claude-usage-by-work-item?var-email=${__value.raw}"
            "&${ds:queryparam}&${work_item:queryparam}&${model:queryparam}&${__url_time_range}")
# Cell "Filter for value" adds an ad hoc filter to every query, which these joins can't take
# (native metrics have no work_item; INFO relabels it). The links above filter instead.
NO_ADHOC = {"matcher": {"id": "byRegexp", "options": ".*"}, "properties": [{"id": "filterable", "value": False}]}
rename = {f"Value #{c[0]}": c[1] for c in cols}
rename.update({"Value #H": "Sessions", "Value #K": "Commits / session", "Value #L": "PRs / session"})
rename.update({"work_item": "Work item", "work_title": "Title", "jira": "Jira", "user.email": "User"})
add({"type": "table", "title": "Usage by work item", "targets": targets,
     "transformations": [{"id": "merge", "options": {}},
                         {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": rename,
                                                        "indexByName": {"work_item": 0, "jira": 1, "work_title": 2, "user.email": 3}}},
                         {"id": "sortBy", "options": {"sort": [{"field": "Cost", "desc": True}]}}],
     "fieldConfig": {"defaults": {"decimals": 1}, "overrides": [
         {"matcher": {"id": "byName", "options": "Cost"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]},
         {"matcher": {"id": "byName", "options": "Tokens"}, "properties": [{"id": "unit", "value": "short"}]},
         {"matcher": {"id": "byName", "options": "Work item"}, "properties": [{"id": "links", "value": [
             {"title": "Filter dashboard to this work item", "url": FILTER_URL}]}]},
         {"matcher": {"id": "byName", "options": "Jira"}, "properties": [{"id": "links", "value": [
             {"title": "Open in Jira", "url": "${jira_base}/${__value.raw}", "targetBlank": True}]}]},
         {"matcher": {"id": "byName", "options": "User"}, "properties": [{"id": "links", "value": [
             {"title": "Filter dashboard to this user", "url": USER_URL}]}]},
         # needed by the filter link, not shown
         {"matcher": {"id": "byName", "options": "work_key"}, "properties": [{"id": "custom.hidden", "value": True}]},
         NO_ADHOC]},
     "options": {"showHeader": True}}, 24, 10)

by_model = [("A", "Cost", per_session("cost.usage", by="model"))] + [
    (ref, name, per_session("token.usage", f'type="{t}"', by="model"))
    for ref, name, t in [("B", "Input", "input"), ("C", "Output", "output"), ("D", "Cache read", "cacheRead"),
                         ("E", "Cache write", "cacheCreation")]]
add({"type": "table", "title": "Cost and tokens by work item and model",
     "description": "Filter with the Model variable to find work items that could move to a cheaper model.",
     "targets": [tgt(joined(e, by="work_item, model", labels="work_item"), ref, fmt="table") for ref, _, e in by_model],
     "transformations": [{"id": "merge", "options": {}},
                         {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {
                             **{f"Value #{r}": n for r, n, _ in by_model}, "work_item": "Work item", "model": "Model"}}},
                         {"id": "sortBy", "options": {"sort": [{"field": "Cost", "desc": True}]}}],
     "fieldConfig": {"defaults": {"unit": "short", "decimals": 1}, "overrides": [
         {"matcher": {"id": "byName", "options": "Cost"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]}, NO_ADHOC]},
     "options": {"showHeader": True}}, 24, 10)

row("Trends")
def ts(title, expr, legend, unit="none", stack=True, interval="1d"):
    return {"type": "timeseries", "title": title, "interval": interval,
            "targets": [tgt(expr, "A", legend, instant=False)],
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"drawStyle": "bars", "fillOpacity": 80,
                            "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]}}}
add(ts("Cost per day by work item", rate_joined("cost.usage"), "{{work_item}}", "currencyUSD"), 12, 8)
add(ts("Claude hours per day by work item", f"({rate_joined('api_seconds')} + {rate_joined('tool_seconds')}) / 3600", "{{work_item}}"), 12, 8)
def bargauge(title, expr, legend, unit, orientation, color, display, name_placement):
    return {"type": "bargauge", "title": title, "targets": [tgt(expr, "A", legend)],
            "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": color},
                                         "thresholds": {"mode": "percentage", "steps": [{"color": "green", "value": 0}]}}},
            "options": {"displayMode": display, "orientation": orientation, "namePlacement": name_placement,
                        "valueMode": "text", "showUnfilled": True, "sizing": "auto",
                        "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}
add(bargauge("Cost by model", joined(per_session("cost.usage", by="model"), by="model", labels="work_item"),
             "{{model}}", "currencyUSD", "horizontal", "continuous-BlPu", "gradient", "top"), 8, 8)
add(bargauge("Tokens by type", joined(per_session("token.usage", by="type"), by="type", labels="work_item"),
             "{{type}}", "short", "vertical", "palette-classic", "basic", "auto"), 8, 8)
add({"type": "piechart", "title": "Claude time: model vs tools", "targets": [
    tgt(f"sum({joined(per_session('api_seconds'))}) / 3600", "A", "model"),
    tgt(f"sum({joined(per_session('tool_seconds'))}) / 3600", "B", "tools")],
    "fieldConfig": {"defaults": {"unit": "h", "color": {"mode": "palette-classic"}}, "overrides": []},
    "options": {"pieType": "donut", "displayLabels": ["percent"], "sort": "desc",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}}}, 4, 8)
cache = stat("Cache-read share of input tokens",
    f'sum({joined(per_session("token.usage", CACHE))}) / sum({joined(per_session("token.usage", INPUTS))})',
    "percentunit", 1)
cache["fieldConfig"]["defaults"].update({"min": 0, "max": 1, "color": {"mode": "thresholds"}, "thresholds": {
    "mode": "absolute", "steps": [{"color": "red", "value": 0}, {"color": "#EAB839", "value": 0.8},
                                  {"color": "green", "value": 0.9}]}})
cache["options"]["colorMode"] = "background"
add(cache, 4, 8)

_MODELS = 'label_values({"__name__"="log_work.usage.cost_usd"}, model)'
_EMAILS = 'query_result(count by ("user.email") (last_over_time({"__name__"="log_work.session.info"}[90d])))'
dash = {
    "title": "Claude usage by work item",
    "uid": "claude-usage-work-item",
    "tags": ["claude-code", "log-work"],
    "timezone": "browser", "schemaVersion": 39, "editable": True,
    "time": {"from": "now-30d", "to": "now"},
    "description": "Claude usage joined to log-work work items on session.id (log_work.session.info). Cost, tokens, lines and Claude time come from the log-work transcript hook (log_work.usage.*, all session types; updates when a session's process stops). Commits and PRs are successful git commit / gh pr create tool calls (including subagents). Your active time comes from Claude Code's native OTel export (terminal sessions only). Where both exist, cost, tokens, lines, commits and PRs use the larger of the hook and native values per session. Turns are prompts you typed; tool calls include subagents. The Model variable filters cost, tokens and session counts. Filter by user with the User variable (user.email on the work-item tag). Cost is Claude Code's own list-price estimate.",
    "templating": {"list": [
        {"type": "datasource", "name": "ds", "label": "Data source", "query": "prometheus", "regex": "/gcp-v2-.*/",
         "current": {"text": "gcp-v2-nonprod", "value": "edq6thuke248we"}},
        {"type": "query", "name": "work_item", "label": "Work item", "datasource": DS,
         "query": {"query": 'label_values({"__name__"="log_work.session.info"}, work_item)', "refId": "wi"},
         "definition": 'label_values({"__name__"="log_work.session.info"}, work_item)',
         "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}, "refresh": 2, "sort": 1},
        {"type": "query", "name": "model", "label": "Model", "datasource": DS,
         "query": {"query": _MODELS, "refId": "mo"}, "definition": _MODELS,
         "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}, "refresh": 2, "sort": 1},
        # "All" (.*) also keeps sessions tagged before user.email was added
        {"type": "query", "name": "email", "label": "User", "datasource": DS,
         "query": {"query": _EMAILS, "refId": "em"}, "definition": _EMAILS, "regex": '/user.email="([^"]+)"/',
         "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}, "refresh": 2, "sort": 1},
        {"type": "constant", "name": "jira_base", "hide": 2, "query": "https://mozilla-hub.atlassian.net/browse"},
    ]},
    "panels": panels,
}
out = sys.argv[1]
json.dump(dash, open(out, "w"), indent=2); open(out, "a").write("\n")
# dump queries for validation
qs = []
for p in panels:
    for t in p.get("targets", []): qs.append((p["title"], t["expr"]))
json.dump(qs, open(out + ".queries.json", "w"))
print(len(panels), "panels,", len(qs), "queries")
