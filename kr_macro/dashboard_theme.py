"""Light and dark presentation for the local dashboard."""

CSS = """
:root{
  color-scheme:light;
  --bg:#F5F5F5;--surface:#FFFFFF;--surface-2:#F1F1F2;--border:#E2E2E4;--grid:#E4E4E7;
  --text:#09090B;--text-2:#52525B;--text-3:#60606B;
  --accent:#075985;--accent-soft:#E0F2FE;--selection:#09090B;--selection-text:#FFFFFF;
  --series-1:#087DA8;--series-2:#405CAD;--series-3:#6C4BB6;--bar:#A6BDD6;
  --good:#10743C;--warn:#8C5000;--bad:#BF2B31;--value-label:#FFFFFF;
  --card:var(--surface);--line:var(--border);--tx:var(--text);--mu:var(--text-3);
  --card-shadow:0 1px 2px rgb(9 9 11 / 7%);
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#18181B;--surface:#27272A;--surface-2:#333339;--border:#52525B;--grid:#3F3F46;
  --text:#FAFAFA;--text-2:#D4D4D8;--text-3:#A1A1AA;
  --accent:#22C9EE;--accent-soft:#1D2C30;--selection:#22C9EE;--selection-text:#18181B;
  --series-1:#22C9EE;--series-2:#A6B9FF;--series-3:#C39BFF;--bar:var(--text-2);
  --good:#62D59A;--warn:#FFBF59;--bad:#FF7770;--value-label:#18181B;
  --card-shadow:none;
}
a{color:var(--accent)}
.tab img[data-chart-theme="dark"]{display:none!important}
:root[data-theme="dark"] .tab img[data-chart-theme="light"]{display:none!important}
:root[data-theme="dark"] .tab img[data-chart-theme="dark"]{display:block!important}
.appearance-menu{display:grid;margin-top:20px;padding-top:14px;border-top:1px solid var(--border)}
.appearance-dialog{width:min(650px,calc(100% - 26px));max-height:calc(100% - 26px);
  overflow:auto;padding:0;color:var(--text);background:var(--surface);
  border:1px solid var(--border);border-radius:17px;box-shadow:0 20px 70px rgb(0 0 0 / 28%)}
.appearance-dialog::backdrop{background:rgb(0 0 0 / 45%)}
.appearance-dialog form{padding:22px}
.appearance-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.appearance-head h2{margin:0;font-size:21px}
.appearance-description{margin:5px 0 16px;color:var(--text-2)}
.appearance-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;
  padding:0;margin:0;border:0;min-width:0}
.appearance-choice{min-width:0;display:grid;gap:9px;cursor:pointer;padding:9px;
  border:1px solid var(--border);border-radius:10px;color:var(--text)}
.appearance-choice:hover{background:var(--surface-2)}
.appearance-choice:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.appearance-choice strong{display:flex;gap:6px;align-items:center;font-size:13px}
.appearance-choice input{margin:0;accent-color:var(--accent)}
.appearance-preview{height:80px;display:grid;grid-template-columns:26% 1fr;gap:7px;padding:7px;
  background:#f5f5f5;border:1px solid var(--border);border-radius:7px;overflow:hidden}
.appearance-preview::before{content:"";border-radius:3px;
  background:repeating-linear-gradient(to bottom,#d4d4d8 0 4px,transparent 4px 10px)}
.appearance-preview i{display:flex;align-items:flex-end;gap:5px;padding:8px;
  border-radius:4px;background:#fff}
.appearance-preview b{width:20%;height:var(--column);background:#087da8;border-radius:2px 2px 0 0}
.appearance-preview b:nth-child(1){--column:24%}
.appearance-preview b:nth-child(2){--column:45%}
.appearance-preview b:nth-child(3){--column:37%}
.appearance-preview b:nth-child(4){--column:72%}
.appearance-preview b:nth-child(5){--column:88%}
.appearance-preview[data-preview="dark"]{background:#18181b}
.appearance-preview[data-preview="dark"]::before{
  background:repeating-linear-gradient(to bottom,#52525b 0 4px,transparent 4px 10px)}
.appearance-preview[data-preview="dark"] i{background:#27272a}
.appearance-preview[data-preview="dark"] b{background:#22c9ee}
.appearance-preview[data-preview="system"]{background:linear-gradient(90deg,#f5f5f5 50%,#18181b 50%)}
.appearance-preview[data-preview="system"] i{background:linear-gradient(90deg,#fff 33%,#27272a 33%)}
.appearance-preview[data-preview="system"] b:nth-child(n+3){background:#22c9ee}
.appearance-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:19px}
.appearance-action{font:inherit;cursor:pointer;color:var(--text);background:var(--surface);
  border:1px solid var(--border);border-radius:8px;padding:6px 12px}
.appearance-action:hover{border-color:var(--accent)}
.appearance-action.apply{background:var(--selection);color:var(--selection-text);border-color:transparent}
@media(max-width:520px){
  .appearance-dialog form{padding:15px}
  .appearance-options{grid-template-columns:1fr;gap:7px}
  .appearance-choice{grid-template-columns:minmax(72px,36%) minmax(0,1fr);align-items:center}
  .appearance-preview{height:54px}
}
@media(max-width:960px){.appearance-menu{margin-top:7px;padding-top:6px;justify-content:start}}
"""

BOOT = """
<script>
const dashboardSettings = new URLSearchParams(location.hash.slice(1));
const themeOptions = ['light', 'dark', 'system'];
const themeMedia = matchMedia('(prefers-color-scheme: dark)');
let storedTheme;
try { storedTheme = localStorage.getItem('kr-macro-color-mode'); } catch {}
const themePreference = [dashboardSettings.get('theme'), storedTheme].find(mode => themeOptions.includes(mode)) || 'light';
const dashboardTheme = themePreference === 'system' ? (themeMedia.matches ? 'dark' : 'light') : themePreference;
document.documentElement.dataset.theme = dashboardTheme;
const dashboardPalette = getComputedStyle(document.documentElement);
const plotColor = name => dashboardPalette.getPropertyValue('--' + name).trim();
</script>
"""

def appearance_menu(icon):
    return f"""
<div class="appearance-menu">
  <button type="button" class="workspace-menu" id="appearance-button" aria-haspopup="dialog"
    aria-controls="appearance-dialog" aria-label="화면 설정" title="화면 설정" hidden>{icon}<span class="workspace-label">화면 설정</span></button>
</div>
"""


def appearance_choice(mode, label):
    columns = "<b></b>" * 5
    return f"""
    <label class="appearance-choice">
      <span class="appearance-preview" data-preview="{mode}" aria-hidden="true"><i>{columns}</i></span>
      <strong><input type="radio" name="appearance" value="{mode}" required> {label}</strong>
    </label>"""


DIALOG = """
<dialog class="appearance-dialog" id="appearance-dialog" aria-labelledby="appearance-title"
  aria-describedby="appearance-description">
  <form method="dialog">
    <div class="appearance-head">
      <h2 id="appearance-title">화면 설정</h2>
      <button class="appearance-action" type="button" data-close-appearance aria-label="닫기">×</button>
    </div>
    <p class="appearance-description" id="appearance-description">이 브라우저에서 사용할 화면 모드를 선택하세요.</p>
    <fieldset class="appearance-options" aria-label="화면 모드">
""" + "".join(
    appearance_choice(mode, label)
    for mode, label in (("light", "라이트 모드"), ("dark", "다크 모드"), ("system", "시스템 설정"))
) + """
    </fieldset>
    <div class="appearance-actions">
      <button class="appearance-action" type="button" data-close-appearance>취소</button>
      <button class="appearance-action apply" type="submit">적용</button>
    </div>
  </form>
</dialog>
"""

SWITCH = DIALOG + """
<script>
const appearanceButton = document.getElementById('appearance-button');
const appearanceDialog = document.getElementById('appearance-dialog');
const appearanceForm = appearanceDialog.querySelector('form');
appearanceButton.hidden = false;
appearanceButton.addEventListener('click', () => {
  const current = appearanceForm.elements.appearance;
  current.value = themePreference;
  appearanceDialog.showModal();
  [...current].find(option => option.checked).focus();
});
for (const button of appearanceDialog.querySelectorAll('[data-close-appearance]')) {
  button.addEventListener('click', () => appearanceDialog.close());
}
appearanceDialog.addEventListener('close', () => appearanceButton.focus());
function reloadDashboardTheme(preference) {
  dashboardSettings.set('theme', preference);
  const active = document.querySelector('.dataset-list button.on');
  if (active) dashboardSettings.set('tab', active.dataset.t);
  location.hash = dashboardSettings.toString();
  location.reload();
}
appearanceForm.addEventListener('submit', event => {
  event.preventDefault();
  const preference = new FormData(appearanceForm).get('appearance');
  if (preference === themePreference) {
    appearanceDialog.close();
    return;
  }
  try { localStorage.setItem('kr-macro-color-mode', preference); } catch {}
  reloadDashboardTheme(preference);
});
if (themePreference === 'system') themeMedia.addEventListener('change', () => reloadDashboardTheme('system'));
const savedTab = dashboardSettings.get('tab');
const viewToRestore = savedTab === 't6'
  ? document.querySelector('.workspace-nav [data-group="reports"]')?.dataset.firstView
  : savedTab;
const selectedTab = [...document.querySelectorAll('.dataset-list button')]
  .find(button => button.dataset.t === viewToRestore);
if (selectedTab && !selectedTab.classList.contains('on')) selectedTab.click();
</script>
"""
