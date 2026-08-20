import re, pathlib

src = pathlib.Path("/Users/john/WorkBuddy/2026-08-12-11-12-59/worktile-comment-board/templates/index.html").read_text()
style = re.search(r"<style>(.*?)</style>", src, re.S).group(1)

toolbar = r"""
<div class="toolbar">
  <div class="tb-card tb-filters">
    <span class="tb-group-label">项目</span>
    <div class="search-select" id="projectPicker">
      <input type="text" id="projectInput" value="全部项目" autocomplete="off" spellcheck="false" readonly style="width:110px">
      <span class="caret">▾</span>
    </div>
    <span class="tb-sep"></span>
    <span class="tb-group-label">负责人</span>
    <div class="search-select" id="ownerPicker">
      <input type="text" id="ownerInput" value="全部负责人" autocomplete="off" spellcheck="false" readonly style="width:110px">
      <span class="caret">▾</span>
    </div>
    <span class="tb-sep"></span>
    <span class="tb-group-label">每页</span>
    <select id="pageSize"><option>50</option></select>
  </div>

  <div class="tb-card tb-search">
    <div class="chip-search" id="chipSearch">
      <div class="search-input" id="titleSearchWrap">
        <input type="text" id="titleSearch" placeholder="搜索任务名称…" spellcheck="false" style="width:100%">
      </div>
      <button id="titleSearchBtn" class="search-go">全局查询</button>
    </div>
  </div>

  <div class="tb-card tb-actions">
    <button class="pill-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2"/><path d="M9 2h6"/></svg>
      <span>延期任务</span>
    </button>
    <button class="pill-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
      刷新
    </button>
    <button class="pill-btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="3.5" cy="6" r="1.2" fill="currentColor"/><circle cx="3.5" cy="12" r="1.2" fill="currentColor"/><circle cx="3.5" cy="18" r="1.2" fill="currentColor"/></svg>
      列设置
    </button>
    <span class="tb-sep"></span>
    <div class="export-switch" id="exportSwitch">
      <button type="button" class="seg">仅任务</button>
      <button type="button" class="seg active">含评论</button>
    </div>
    <button class="pill-btn primary">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      导出 Excel
    </button>
  </div>
</div>
"""

page = f"""<!doctype html><html><head><meta charset="utf-8"><style>{style}</style></head>
<body style="background:var(--bg-grad);padding:24px;margin:0;">
<div style="font:600 15px var(--ff);color:var(--text);margin-bottom:14px;">任务评论区看板</div>
{toolbar}
</body></html>"""
pathlib.Path("/tmp/wt_toolbar_v2.html").write_text(page, encoding="utf-8")
print("written /tmp/wt_toolbar_v2.html")
