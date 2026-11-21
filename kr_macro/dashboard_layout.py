"""Navigation and data workspace for the local dashboard."""

from base64 import b64encode
from html import escape
from pathlib import Path

from dashboard_theme import appearance_menu
import industry_exports


ICONS = {
    "market": "↗", "sectors": "▦", "flow": "⇄", "funding": "▤", "trade": "▥", "memory": "▣", "companies": "▤",
    "reports": "▱", "calendar": "▦", "appearance": "◐", "collapse": "«", "expand": "»",
}
ICON_FILES = ((".svg", "image/svg+xml"), (".png", "image/png"), (".webp", "image/webp"))
# Source image width and the square viewport enclosing the glyph (left, top, size).
ICON_VIEWPORTS = {
    "flow": (126, 38, 13, 56),
    "companies": (135, 37, 18, 59),
    "trade": (155, 52, 29, 55), "memory": (166, 62, 34, 52),
    "funding": (125, 31, 29, 62),
    "market": (140, 46, 28, 52), "calendar": (160, 63, 22, 55),
    "reports": (141, 46, 26, 58),
}


def toolbar_icon(name):
    directory = Path(__file__).resolve().parent / "icons"
    for suffix, mime in ICON_FILES:
        asset = directory / (name + suffix)
        if asset.is_file():
            encoded = b64encode(asset.read_bytes()).decode("ascii")
            return (f'<img class="workspace-icon" data-icon="{name}" alt="" aria-hidden="true" '
                    f'src="data:{mime};base64,{encoded}">')
    source = directory / (name + ".source.png")
    if name in ICON_VIEWPORTS and source.is_file():
        width, x, y, size = ICON_VIEWPORTS[name]
        encoded = b64encode(source.read_bytes()).decode("ascii")
        position = f"width:{width / size:.4%};left:{-x / size:.4%};top:{-y / size:.4%}"
        return (f'<span class="workspace-icon workspace-icon-viewport" data-icon="{name}" aria-hidden="true">'
                f'<img alt="" src="data:image/png;base64,{encoded}" style="{position}"></span>')
    return f'<span class="workspace-icon" data-icon="{name}" aria-hidden="true">{ICONS[name]}</span>'


def navigation(companies, company_search="", *, flow_etfs=()):
    groups = [
        ("market", "시장·매크로", [
            ("t1", "장세 판단", ""),
        ]),
        ("sectors", "섹터 히트맵", [
            ("sector-heatmap", "섹터 히트맵", ""),
        ]),
        ("flow", "수급", [("stock-flow-all", "전체 종목", "코스피 100 · 코스닥 50")] + [
            (f'flow-{etf["ticker"]}', etf["name"], etf["ticker"])
            for etf in flow_etfs
        ]),
        ("funding", "자금", [
            ("t4", "신용잔고·예탁금", "시장별 신용잔고와 투자자예탁금"),
        ]),
        ("trade", "수출", [
            ("t5", "반도체 수출", "전체 수출과 품목별 실적"),
            ("cosmetics", "화장품 수출", "화장품·미용 품목별 실적"),
            *industry_exports.navigation(),
        ]),
        ("memory", "메모리", [
            ("memory", "DDR5 현물가", "DDR5 16Gb 칩 가격 추이"),
        ]),
        ("companies", "기업 분석", [
            (f's-{company["id"]}', company["tab"], "")
            for company in companies
        ] + ([("s-catalog", "종목 상세", "검색 결과")] if company_search else [])),
        ("reports", "증권사 리포트", [
            (f'r-{company["id"]}', company["name"], "투자의견·목표주가·리포트")
            for company in companies
        ] + ([("r-catalog", "종목 리포트", "검색 결과")] if company_search else [])),
        ("calendar", "경제 일정", [
            ("t7", "경제 일정", "주요 지표 발표와 기업 일정"),
        ]),
    ]
    group_buttons, dataset_buttons = [], []
    for group, name, datasets in groups:
        if not datasets:
            continue
        first_view = escape(datasets[0][0])
        label = escape(name)
        selected = group == "market"
        group_buttons.append(
            f'<button type="button" class="workspace-menu" data-group="{group}" '
            f'data-first-view="{first_view}" aria-pressed="{str(selected).lower()}" '
            f'aria-label="{label}" title="{label}">'
            f'{toolbar_icon(group)}<span class="workspace-label">{label}</span></button>'
        )
        for view, label, description in datasets:
            active = view == "t1"
            visible = "" if selected else " hidden"
            state = ' class="on" aria-current="page"' if active else ""
            description_html = f"<small>{escape(description)}</small>" if description else ""
            dataset_buttons.append(
                f'<button type="button" data-t="{escape(view)}" data-group="{group}" '
                f'aria-controls="{escape(view)}"{state}{visible}>'
                f'<span>{escape(label)}</span>{description_html}</button>'
            )
    nav = ('<aside class="dashboard-sidebar" id="dashboard-sidebar">'
           '<div class="toolbar-head">'
           '<div class="dashboard-brand"><span aria-hidden="true">국장</span><strong>매크로 대시보드</strong></div>'
           '<button class="toolbar-toggle" id="toolbar-toggle" type="button" aria-expanded="true" '
           'aria-controls="dashboard-sidebar" aria-label="메뉴 접기" title="메뉴 접기" hidden>'
           + toolbar_icon("collapse") + toolbar_icon("expand") + '</button></div>'
           '<nav class="workspace-nav" aria-label="데이터 분야">'
           + "".join(group_buttons) + '</nav>' + appearance_menu(toolbar_icon("appearance")) + '</aside>')
    datasets = ('<aside class="dataset-pane" aria-labelledby="dataset-heading">'
                '<h2 id="dataset-heading">시장·매크로</h2>'
                + company_search +
                '<nav class="dataset-list" aria-label="분야별 데이터">'
                + "".join(dataset_buttons) + '</nav></aside>')
    return nav, datasets


HEADER = """
<div class="workspace-masthead">
  <div class="workspace-heading">
    <div class="workspace-section" id="workspace-section">시장·매크로</div>
    <h1 id="workspace-title">장세 판단</h1>
  </div>
</div>
"""


CSS = """
body{font-size:14px}
.dashboard-shell{display:grid;grid-template-columns:218px minmax(0,1fr);min-height:100vh;
  max-width:1920px;margin:0 auto}
.dashboard-sidebar{position:sticky;top:0;align-self:start;min-height:100vh;min-width:0;overflow:hidden;
  border-right:1px solid var(--border);padding:20px 9px;background:var(--surface)}
.toolbar-head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;margin-bottom:20px}
.dashboard-brand{display:flex;align-items:center;gap:7px;font-size:14px;line-height:1.2}
.dashboard-brand span{color:var(--selection-text);background:var(--selection);
  border-radius:8px;padding:5px 7px;font-size:12px;font-weight:700;white-space:nowrap}
.dashboard-brand strong,.workspace-label{white-space:nowrap}
.workspace-nav{display:grid;gap:5px}
.workspace-menu,.toolbar-toggle{display:flex;align-items:center;gap:9px;font:inherit;cursor:pointer;
  text-align:left;border:0;border-radius:8px;background:transparent;color:var(--text-2);padding:10px 11px}
.workspace-menu{min-height:42px;min-width:0;overflow:hidden}
.toolbar-toggle{padding:6px;border:1px solid var(--border);justify-content:center}
.toolbar-toggle:hover{background:var(--accent-soft);color:var(--accent)}
.workspace-icon{display:inline-grid;flex:0 0 20px;width:20px;height:20px;place-items:center;
  object-fit:contain;line-height:1;font-size:18px;font-weight:600}
.workspace-icon-viewport{position:relative;overflow:hidden;mix-blend-mode:multiply;opacity:.8}
.workspace-icon-viewport img{position:absolute;display:block;max-width:none;filter:contrast(1.65) invert(1)}
:root[data-theme="dark"] .workspace-icon-viewport{mix-blend-mode:screen}
:root[data-theme="dark"] .workspace-icon-viewport img{filter:contrast(1.65)}
.toolbar-toggle [data-icon="expand"]{display:none}
.workspace-menu:hover,.workspace-menu[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent)}
.workspace-menu[aria-pressed="true"]{font-weight:700}
:root[data-theme="dark"] .dashboard-sidebar{background:var(--bg)}
:root[data-theme="dark"] .dashboard-brand span{background:var(--text-2);color:var(--bg)}
:root[data-theme="dark"] .dashboard-sidebar :is(.workspace-menu,.toolbar-toggle):hover{
  background:color-mix(in srgb,var(--surface) 60%,var(--bg));color:var(--text)}
:root[data-theme="dark"] .dashboard-sidebar .workspace-menu[aria-pressed="true"]{
  background:var(--surface);color:var(--text)}
:root[data-theme="dark"] .dashboard-sidebar .workspace-icon-viewport{opacity:.62}
:root[data-theme="dark"] .dashboard-sidebar .workspace-menu:is(:hover,[aria-pressed="true"]) .workspace-icon-viewport{opacity:.98}
:root[data-theme="dark"] .dashboard-sidebar button:focus-visible{outline-color:var(--text-2)}
.dashboard-shell[data-sidebar="collapsed"]{grid-template-columns:64px minmax(0,1fr)}
.dashboard-shell[data-sidebar="collapsed"] .toolbar-toggle [data-icon="collapse"]{display:none}
.dashboard-shell[data-sidebar="collapsed"] .toolbar-toggle [data-icon="expand"]{display:inline-grid}
@media (min-width:961px){
  .toolbar-head{display:block;position:relative;height:70px;padding-inline:4px}
  .dashboard-brand{height:34px;overflow:hidden;max-width:100%;padding-right:40px}
  .dashboard-brand strong{white-space:normal}
  .toolbar-toggle{position:absolute;top:0;right:4px}
  .dashboard-shell[data-sidebar="collapsed"] .toolbar-toggle{top:36px}
  .dashboard-shell[data-sidebar="collapsed"] .dashboard-brand{padding-right:0}
  .dashboard-brand strong,.workspace-label{transition:opacity 110ms ease}
  .dashboard-shell[data-sidebar="collapsed"] :is(.workspace-label,.dashboard-brand strong){opacity:0}
  .dashboard-shell[data-sidebar-moving] .tab.on :is(.cchart,.xchart,.cosm-chart,.ichart,.fchart){overflow:clip}
}
@media (min-width:961px) and (prefers-reduced-motion:no-preference){
  .dashboard-shell[data-sidebar-moving]{transition:grid-template-columns 210ms cubic-bezier(.2,.65,.35,1)}
  .dashboard-shell[data-sidebar-moving] .toolbar-toggle{transition:top 210ms cubic-bezier(.2,.65,.35,1)}
}
@media (prefers-reduced-motion:reduce){
  .dashboard-brand strong,.workspace-label{transition:none}
}
.dashboard-main{min-width:0;padding:24px clamp(18px,2.5vw,40px) 52px}
.workspace-masthead{display:flex;align-items:center;justify-content:space-between;gap:16px}
.workspace-section{font-size:12px;font-weight:700;color:var(--accent);margin-bottom:3px}
.workspace-heading h1{font-size:clamp(26px,3vw,36px);line-height:1.25;letter-spacing:-.04em;margin:0}
.dashboard-main>.meta{margin-top:10px}
.workspace-grid{display:grid;grid-template-columns:minmax(184px,220px) minmax(0,1fr);gap:20px;
  align-items:start;margin-top:20px}
.workspace-grid:has(> .workspace-detail > :is(#t1,#t4,#investor-flow).on){grid-template-columns:minmax(0,1fr)}
.workspace-grid:has(> .workspace-detail > :is(#t1,#t4,#investor-flow).on)>.dataset-pane{display:none}
.dataset-pane{min-width:0;position:sticky;top:18px;align-self:start;
  padding:15px;background:var(--surface);border:1px solid var(--border);border-radius:14px}
.dataset-pane h2{margin:1px 6px 12px;font-size:14px;color:var(--text);font-weight:700}
.dataset-list{display:grid;gap:5px}
.dataset-list button{display:grid;gap:4px;text-align:left;width:100%;font:inherit;
  background:transparent;color:var(--text);border:1px solid transparent;border-radius:8px;
  padding:9px 10px;cursor:pointer}
.dataset-list button span{font-weight:650}
.dataset-list button small{color:var(--text-3);font-size:11px;line-height:1.5}
.dataset-list button:hover{background:var(--surface-2)}
.dataset-list button.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
:root[data-theme="dark"] .dataset-list button:hover{
  background:color-mix(in srgb,var(--surface-2) 55%,var(--surface))}
:root[data-theme="dark"] .dataset-list button.on{
  background:var(--surface-2);border-color:transparent;color:var(--text)}
:root[data-theme="dark"] .dataset-list button.on small{color:var(--text-2)}
:root[data-theme="dark"] .dataset-list button:focus-visible{outline-color:var(--text-2)}

.workspace-grid:has(> .workspace-detail > :is(.flow-etf,#stock-flow-all).on) .dataset-list{
  max-height:max(220px,calc(100dvh - 90px));overflow:auto}
@media (max-width:730px){
  .workspace-grid:has(> .workspace-detail > :is(.flow-etf,#stock-flow-all).on) .dataset-list{max-height:210px}
}
.flow-etf-date{margin:0;color:var(--text-2);font-size:12px}
.flow-etf-facts{display:flex;flex-wrap:wrap;gap:12px 30px;margin:12px 0 0;font-variant-numeric:tabular-nums}
.flow-etf-facts dt{color:var(--text-2);font-size:12px}
.flow-etf-facts dd{margin:4px 0 0;font-weight:700;font-size:17px}
.flow-etf-facts [data-change="up"]{color:var(--bad)}
.flow-etf-facts [data-change="down"]{color:var(--series-1)}
.workspace-detail{min-width:0}
.workspace-detail .tab>.card{border-radius:16px}
.workspace-detail .gcards{grid-template-columns:repeat(2,minmax(0,1fr))}
.workspace-detail .fkpis{grid-template-columns:repeat(2,minmax(0,1fr))}
@media (max-width:1120px){
  .dashboard-shell{grid-template-columns:184px minmax(0,1fr)}
  .dashboard-brand{font-size:12px;gap:6px}
  .workspace-grid{grid-template-columns:172px minmax(0,1fr);gap:14px}
  .dataset-pane{padding:10px}
}
@media (max-width:960px){
  .dashboard-shell{display:block}
  .dashboard-sidebar{position:static;min-height:0;border:0;border-bottom:1px solid var(--border);
    padding:12px 18px}
  .toolbar-head{margin-bottom:10px}
  .workspace-nav{display:flex;flex-wrap:wrap}
  .workspace-menu{padding:7px 9px}
  .dashboard-shell[data-sidebar="collapsed"] .dashboard-brand strong,
  .dashboard-shell[data-sidebar="collapsed"] .workspace-label{display:none}
  .dashboard-shell[data-sidebar="collapsed"] .workspace-menu{width:44px;justify-content:center}
}
@media (max-width:730px){
  .dashboard-main{padding:16px 12px 30px}
  .workspace-grid{display:block}
  .dataset-pane{position:static;margin-bottom:14px}
  .dataset-list{grid-template-columns:repeat(auto-fit,minmax(124px,1fr))}
}
@media (min-width:731px){
  .workspace-grid:has(> .dataset-pane #company-search:not([hidden])){
    grid-template-columns:clamp(220px,23vw,276px) minmax(0,1fr)
  }
}
@media (max-width:460px){
  .workspace-detail .gcards{grid-template-columns:1fr}
  .workspace-detail .fkpis{grid-template-columns:repeat(2,minmax(0,1fr))}
}
"""


SCRIPT = """
<script>
const workspaceButtons = [...document.querySelectorAll('.workspace-nav [data-group]')];
const datasetButtons = [...document.querySelectorAll('.dataset-list [data-t]')];
const workspaceTitle = document.getElementById('workspace-title');
const workspaceSection = document.getElementById('workspace-section');
const datasetHeading = document.getElementById('dataset-heading');
const dashboardShell = document.querySelector('.dashboard-shell');
const toolbarToggle = document.getElementById('toolbar-toggle');
let collapsed = false;
let sidebarMotion = 0;
try { collapsed = localStorage.getItem('kr-macro-sidebar-collapsed') === 'true'; } catch {}

function setSidebar(collapsed) {
  dashboardShell.dataset.sidebar = collapsed ? 'collapsed' : 'expanded';
  const label = collapsed ? '메뉴 펼치기' : '메뉴 접기';
  toolbarToggle.setAttribute('aria-expanded', String(!collapsed));
  toolbarToggle.setAttribute('aria-label', label);
  toolbarToggle.title = label;
}
setSidebar(collapsed);
toolbarToggle.hidden = false;
function resizeActiveSidebarCharts() {
  if (typeof echarts === 'undefined') return;
  const visibleCharts = document.querySelectorAll('.workspace-detail .tab.on :is(.cchart,.xchart,.cosm-chart,.ichart,.fchart)');
  for (const container of visibleCharts) {
    const chart = echarts.getInstanceByDom(container);
    if (chart && container.clientWidth > 0) chart.resize();
  }
}
toolbarToggle.addEventListener('click', () => {
  const motion = ++sidebarMotion;
  collapsed = !collapsed;
  dashboardShell.setAttribute('data-sidebar-moving', '');
  setSidebar(collapsed);
  try { localStorage.setItem('kr-macro-sidebar-collapsed', String(collapsed)); } catch {}
  requestAnimationFrame(async () => {
    if (motion !== sidebarMotion) return;
    const transition = dashboardShell.getAnimations().find(animation => animation.transitionProperty === 'grid-template-columns');
    if (transition) await transition.finished.catch(() => {});
    if (motion !== sidebarMotion) return;
    dashboardShell.removeAttribute('data-sidebar-moving');
    resizeActiveSidebarCharts();
  });
});

function selectWorkspaceView(active) {
  const group = active.dataset.group;
  const title = active.querySelector('span').textContent;
  const label = workspaceButtons.find(button => button.dataset.group === group).getAttribute('aria-label');
  for (const button of workspaceButtons) {
    button.setAttribute('aria-pressed', String(button.dataset.group === group));
  }
  for (const button of datasetButtons) {
    button.hidden = button.dataset.group !== group;
    if (button === active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  workspaceTitle.textContent = title;
  workspaceSection.textContent = label;
  datasetHeading.textContent = label;
  const companySearch = document.getElementById('company-search');
  if (companySearch) companySearch.hidden = group !== 'companies' && group !== 'reports';
  window.setCompanyDirectoryGroup?.(group);
}

const viewForGroup = new Map(workspaceButtons.map(button => [button.dataset.group, button.dataset.firstView]));
function openWorkspaceView(id) {
  const button = datasetButtons.find(button => button.dataset.t === id);
  if (button) button.click();
}
for (const menu of workspaceButtons) {
  menu.addEventListener('click', () => {
    openWorkspaceView(viewForGroup.get(menu.dataset.group));
    window.scrollTo(0, 0);
  });
}
for (const button of datasetButtons) {
  button.addEventListener('click', () => window.scrollTo(0, 0));
  button.addEventListener('click', () => viewForGroup.set(button.dataset.group, button.dataset.t));
}
</script>
"""
