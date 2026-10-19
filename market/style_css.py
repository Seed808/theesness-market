# Copied from theesness-data (edgemodels/site.py) so both sites match, plus card styles.
CSS = r"""
:root{
  /* Layout: a scoreboard ledger. Narrow reading column, game rows as ruled tiles, numbers in mono. */
  --bg:#eef1f4; --panel:#ffffff; --ink:#121820; --muted:#5b6672; --rule:#d5dbe2;
  --model:#1d4fd1; --market:#6b7480; --edge:#b86a00; --win:#1f7a4a; --loss:#b3352b;
  --display:"Barlow Condensed","Arial Narrow",Arial,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f1318; --panel:#171c23; --ink:#e7ebf0; --muted:#94a0ad; --rule:#2a323c;
  --model:#7ea2ff; --market:#9aa4b0; --edge:#f0a53a; --win:#4fc283; --loss:#f07a6f; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f1318; --panel:#171c23; --ink:#e7ebf0; --muted:#94a0ad; --rule:#2a323c;
  --model:#7ea2ff; --market:#9aa4b0; --edge:#f0a53a; --win:#4fc283; --loss:#f07a6f; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.5;margin:0}
.wrap{max-width:1080px;margin:0 auto;padding-inline:16px;padding-block:0 48px}
a{color:var(--model)} a:focus-visible{outline:2px solid var(--model);outline-offset:2px}
header.top{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;justify-content:space-between;padding-block:18px;border-bottom:2px solid var(--ink)}
.brand{font-family:var(--display);font-weight:700;font-size:26px;letter-spacing:.02em;text-transform:uppercase;color:var(--ink);text-decoration:none}
.brand b{color:var(--model)}
nav.sports{display:flex;flex-wrap:wrap;gap:6px}
nav.sports a{font-family:var(--display);font-weight:600;font-size:16px;letter-spacing:.06em;text-decoration:none;color:var(--ink);padding:4px 10px;border:1px solid var(--rule);border-radius:3px}
nav.sports a.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
nav.weeks{margin-top:18px}
nav.weeks a{font-family:var(--mono);font-size:12px;font-weight:500;letter-spacing:.04em}
h1{font-family:var(--display);font-weight:700;font-size:clamp(34px,6vw,54px);line-height:1;margin:28px 0 10px;text-wrap:balance;text-transform:uppercase}
h2{font-family:var(--display);font-weight:600;font-size:22px;letter-spacing:.04em;text-transform:uppercase;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
.lede{max-width:66ch;color:var(--muted)}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:18px 0}
.tile{background:var(--panel);border:1px solid var(--rule);padding:12px 14px;text-decoration:none;color:var(--ink);display:flex;flex-direction:column;gap:2px}
.tile .k{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.tile .v{font-family:var(--display);font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}
.tile .s{font-size:13px;color:var(--muted)}
.day{margin-top:26px}
.board{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px}
.game{background:var(--panel);border:1px solid var(--rule);padding:12px 14px;display:flex;flex-direction:column;gap:8px;min-width:0}
.game.hot{border-color:var(--edge);box-shadow:inset 3px 0 0 var(--edge)}
.game.hot2{box-shadow:inset 6px 0 0 var(--edge);background:color-mix(in srgb,var(--edge) 7%,var(--panel))}
.badge{display:inline-block;font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.06em;padding:1px 6px;border-radius:3px;border:1px solid var(--rule);color:var(--muted);margin-right:6px;vertical-align:1px}
.badge.gap{border-color:var(--edge);color:var(--edge)} .badge.gap2{background:var(--edge);border-color:var(--edge);color:var(--bg)}
.badge.none{border-style:dashed}
.upd{font-family:var(--mono);font-size:11.5px;color:var(--muted)} .upd b{color:var(--ink);font-weight:600}
.gaps{background:var(--panel);border:1px solid var(--edge);padding:12px 14px;margin:14px 0}
.gaps ol{margin:6px 0 4px;padding-left:22px;font-family:var(--mono);font-size:13px} .gaps li{margin:3px 0}
.meta{font-family:var(--mono);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.teamrow{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:baseline}
.teamrow .t{font-weight:600}
.teamrow .t small{color:var(--muted);font-weight:400;margin-left:6px;font-family:var(--mono);font-size:12px}
.teamrow .sc{font-family:var(--display);font-weight:700;font-size:26px;font-variant-numeric:tabular-nums}
dl.lines{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:4px 0 0;font-size:13px}
dl.lines dt{color:var(--muted)} dl.lines dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums}
.pick{color:var(--edge);font-weight:600}
.more{font-family:var(--mono);font-size:12px;letter-spacing:.06em;text-transform:uppercase;text-decoration:none}
.numbers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
@media (max-width:560px){.numbers{grid-template-columns:1fr}}
.scorebig{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;background:var(--panel);border:1px solid var(--rule);padding:16px}
.scorebig .n{font-family:var(--display);font-size:clamp(40px,9vw,64px);font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.scorebig .r{text-align:right}
.bars{display:flex;flex-direction:column;gap:6px;background:var(--panel);border:1px solid var(--rule);padding:14px}
.bar{display:grid;grid-template-columns:minmax(110px,170px) 1fr 52px;gap:10px;align-items:center;font-size:13px}
.track{position:relative;height:14px;background:linear-gradient(var(--rule),var(--rule)) center/1px 100% no-repeat}
.fill{position:absolute;top:2px;height:10px;background:var(--model)}
.fill.away{background:var(--market)}
.bar .val{font-family:var(--mono);text-align:right;font-variant-numeric:tabular-nums}
.axis{display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;color:var(--muted);padding-inline:calc(min(170px,30%) + 10px) 62px}
.note{font-size:13px;color:var(--muted);max-width:70ch}
.tbl{overflow-x:auto;background:var(--panel);border:1px solid var(--rule)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--rule);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:500}
th:first-child,td:first-child{text-align:left}
td.mono{font-family:var(--mono)}
.pct{display:inline-grid;grid-template-columns:60px 28px;gap:6px;align-items:center}
.pct i{display:block;height:6px;background:var(--rule);position:relative}
.pct i b{position:absolute;inset:0 auto 0 0;background:var(--model)}
.res-VOID{color:var(--muted);font-weight:600}.res-WIN{color:var(--win);font-weight:600}.res-LOSS{color:var(--loss);font-weight:600}.res-PUSH{color:var(--muted)}
.hist{display:inline-flex;gap:1px;align-items:flex-end;height:22px;vertical-align:middle}
.hist span{width:5px;background:var(--model);opacity:.75}
.flag{font-family:var(--mono);font-size:11px;padding:1px 6px;border:1px solid var(--edge);color:var(--edge);border-radius:2px}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--rule);font-size:12px;color:var(--muted)}

.card{background:var(--panel);border:1px solid var(--rule);padding:12px 14px;display:flex;flex-direction:column;gap:8px;min-width:0}
.card.aplus{border-color:var(--edge);box-shadow:inset 6px 0 0 var(--edge)}
.card .head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.card .tk{font-family:var(--display);font-weight:700;font-size:26px;letter-spacing:.02em}
.card .ty{font-family:var(--mono);font-size:13px;font-weight:600}
.ty.PUT{color:var(--loss)} .ty.CALL{color:var(--win)}
.conf{font-family:var(--display);font-weight:700;font-size:22px;font-variant-numeric:tabular-nums}
.why{font-size:13px;color:var(--muted)}
.status{font-family:var(--mono);font-size:11.5px;letter-spacing:.04em}
.bias{background:var(--panel);border:1px solid var(--rule);padding:12px 14px}
.bias .reg{font-family:var(--display);font-weight:700;font-size:30px;letter-spacing:.03em}
.bias ul{margin:6px 0 0;padding-left:18px;font-size:13px} .bias li{margin:2px 0}
.empty{background:var(--panel);border:1px dashed var(--rule);padding:16px;color:var(--muted)}
"""
