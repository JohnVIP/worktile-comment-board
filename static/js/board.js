// ====================== 列定义元数据 ======================
// 端点锚定：项目名称（最左） + 最近更新时间（最右） 都是端点列 ——
// 永远在两端、永远可见、不可被其他列拖到中间；端点列不加 resizer，
// 只有"中间的列"才允许通过拖拽手柄调节列宽。
// 模式：fixed=固定列宽（可拖拽调节），auto=按内容自适应列宽。
// 列默认宽度（按截图实测：项目 200 / 编号 110 / 名称 270 / 负责人 130 / 评论 280 / 时间 90；
// fixed 列（项目/评论）硬按此值；auto 列（编号/名称/负责人/时间）按内容自适应，
// 实际渲染中 content 决定最终宽度，DEFAULT_COL_WIDTHS 仅在回退场景使用）
const COLS_META = [
  { key: "proj",     label: "项目名称",     width: 200, anchor: true,    mode: "fixed" },
  { key: "code",     label: "任务编号",     width: 110,               mode: "auto"  },
  { key: "title",    label: "任务名称",     width: 270,               mode: "auto"  },
  { key: "desc",     label: "描述",        width: 240,               mode: "fixed" },
  { key: "status",   label: "状态",        width: 90,                mode: "auto"  },
  { key: "start",    label: "开始时间",     width: 170,               mode: "auto"  },
  { key: "owner",    label: "任务负责人",   width: 130,               mode: "auto"  },
  { key: "due",      label: "截止时间",     width: 150,               mode: "auto"  },  // 普通看板 + 延期视图均显示（可在列设置关闭）
  { key: "overdue",  label: "逾期天数",     width: 90,                mode: "auto"  },  // 普通看板 + 延期视图均显示（可在列设置关闭）
  { key: "comments", label: "评论",        width: 470,               mode: "fixed" },
  { key: "updated",  label: "最近更新时间", width: 90,  endAnchor: true, mode: "auto" },
];
const DEFAULT_COL_MODES = Object.fromEntries(COLS_META.map(c => [c.key, c.mode]));

// 列宽最小值（自适应/固定列都遵守，避免列窄到看不见）
const COL_MIN_WIDTH = 36;

const COLUMN_CONFIG_KEY = "wt-board:column-config:v2";   // v2: 增加 modes 字段
const COL_WIDTHS_KEY    = "wt-board:col-widths:v2";      // 沿用 v2 键；索引按渲染时的当前 col 下标
const COL_WIDTHS_SCHEMA = 5;                             // 列宽存储 schema：v4 新增 状态/开始时间 两列；v5 开始时间格式改 年月日时分秒（默认列宽加宽）
const DEFAULT_COL_ORDER = COLS_META.map(c => c.key);
const DEFAULT_COL_WIDTHS = Object.fromEntries(COLS_META.map(c => [c.key, c.width]));

const state = {
  projectId: "__all__",
  projectName: "全部项目",
  projects: [],       // 项目列表缓存（来自 /api/projects）
  // 负责人筛选：null=全部 / "__unassigned__"=未分配 / {uid, name}=具体成员
  // 负责人列表来自 /api/members，按 uid 匹配（name 中文相同也可能存在重复 uid）
  ownerFilter: null,
  // 导出模式：comments=含评论（默认）/ tasks=仅任务（不含评论 sheet，更快）
  exportMode: "comments",
  members: [],        // 负责人列表缓存（来自 /api/members），[{uid, name}]
  // 切换项目时清空 ownerFilter（避免跨项目残留不同成员造成"筛了等于没筛"的困惑）
  // ownerFilter 重置在 selectProject 内执行。
  tenantHost: "techwll.worktile.com",  // 任务详情页跳转用的租户域名（来自 /api/me 或登录响应）
  clientIdMasked: "",    // 当前租户打码后的 client_id（隔离标识，来自 /api/me 或登录响应）
  tenantFingerprint: "", // 当前租户指纹（隔离证明，同一租户恒定、不同租户不同）
  // 任务名称搜索：typeahead + 多选 chip + OR 查询
  titleChips: [],         // 已选 chip 列表（每项 {task_id, identifier, title, project_name}）
  titleDraft: "",         // 输入框当前草稿（未选中前）
  titleCandidates: [],    // 当前候选列表（typeahead）
  titleIdx: -1,           // 候选下拉键盘高亮下标
  titleDebounceTimer: null,
  titleDebounceMs: 100,
  page: 0,
  pageSize: 50,
  total: null,
  hasMore: false,
  expanded: new Set(),
  commentsCache: {},   // taskId -> comments array
  commentsExpanded: new Set(),   // 已展开全部评论的 taskId（默认只显最近 COMMENTS_PREVIEW 条）
  colWidths: {},       // 用户自定义列宽（按当前渲染顺序的 col index -> px），来自 localStorage
  colOrder: [...DEFAULT_COL_ORDER],            // 当前列展示顺序（proj 始终在 [0]）
  colVisible: Object.fromEntries(COLS_META.map(c => [c.key, true])),  // 每列显隐
  colModes: { ...DEFAULT_COL_MODES },          // 每列宽度模式：fixed=固定可拖 / auto=按内容自适应
  // 视图模式：board=普通看板，overdue=延期任务视图（仅后者显示 截止时间/逾期天数 两列）
  view: "board",
  // 主看板布局快照（进入逾期视图前保存，切回时恢复，互不污染）
  boardColOrder: null,
  boardColVisible: null,
  boardColModes: null,
  overdueForceRefresh: false,   // 逾期视图点「刷新」时置 true，绕过缓存
};

// =================== 列宽拖拽调节 ===================
// 注：COL_WIDTHS_KEY 已在 COLS_META 块顶部声明（line 631），避免重复声明。

function loadColumnWidths(){
  // schema 迁移：v3 之前 colWidths 按下标（render order）存储；改列顺序后下标错位会导致宽度错乱。
  // 新版写入时附带 __schema 字段；读取时若缺字段即视为旧版本，自动清空。
  try {
    const raw = localStorage.getItem(COL_WIDTHS_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object"){
      if (obj.__schema === COL_WIDTHS_SCHEMA){
        const { __schema, ...rest } = obj;
        state.colWidths = rest;
      } else {
        state.colWidths = {};  // 旧版本：列顺序或默认值已变，丢弃旧宽度让用户用新默认值
      }
    }
  } catch(e){ /* localStorage 可能被禁用，忽略 */ }
  // 迁移：旧版本可能给评论列存了 380px，导致整表溢出容器。
  // 强制重置为默认宽度（250px），让三遍法重新规划列宽预算。
  try {
    const commentsIdx = COLS_META.findIndex(c => c.key === "comments");
    if (commentsIdx >= 0 && state.colWidths[commentsIdx] !== undefined) {
      delete state.colWidths[commentsIdx];
    }
  } catch(e){}
}

function saveColumnWidths(){
  try {
    localStorage.setItem(COL_WIDTHS_KEY, JSON.stringify({ ...state.colWidths, __schema: COL_WIDTHS_SCHEMA }));
  } catch(e){}
}

// =================== 列配置：显示哪些列、按什么顺序 ===================
/** 从 localStorage 恢复列配置；任何字段缺失/非法都回退默认；保证 proj 始终 [0] + 可见。 */
function loadColumnConfig(){
  let hadSaved = false;   // 是否存在用户保存过的列配置（决定是否应用窄屏默认列集）
  let obj = null;
  try {
    const raw = localStorage.getItem(COLUMN_CONFIG_KEY);
    if (raw){
      hadSaved = true;
      obj = JSON.parse(raw);
    }
    if (obj && Array.isArray(obj.order)){
      // 与 DEFAULT_COL_ORDER 求并集、按用户顺序 + 默认补漏，剔除未知 key
      const seen = new Set();
      const merged = [];
      for (const k of obj.order){
        if (typeof k === "string" && COLS_META.some(c => c.key === k) && !seen.has(k)){
          merged.push(k); seen.add(k);
        }
      }
      for (const k of DEFAULT_COL_ORDER){
        if (!seen.has(k)){ merged.push(k); seen.add(k); }
      }
      // 强制 proj 永远在第一位，updated 永远在最后一位
      merged.sort((a, b) => {
        if (a === "proj") return -1;
        if (b === "proj") return 1;
        if (a === "updated") return 1;
        if (b === "updated") return -1;
        return 0;
      });
      state.colOrder = merged;
    }
    if (obj && obj.visible && typeof obj.visible === "object"){
      for (const c of COLS_META){
        if (typeof obj.visible[c.key] === "boolean"){
          state.colVisible[c.key] = obj.visible[c.key];
        }
      }
    }
    // 加载 modes：每列固定/自适应（v2 才有，缺失时回退到默认）
    if (obj && obj.modes && typeof obj.modes === "object"){
      for (const c of COLS_META){
        if (obj.modes[c.key] === "fixed" || obj.modes[c.key] === "auto"){
          state.colModes[c.key] = obj.modes[c.key];
        }
      }
    }
  } catch(e){ /* localStorage 异常时沿用默认 */ }
  // 强制左端点 proj：永远在第一个、永远可见、固定列宽（这是分组合并视觉的硬约束）
  state.colOrder = ["proj", ...state.colOrder.filter(k => k !== "proj")];
  state.colVisible["proj"] = true;
  state.colModes["proj"] = "fixed";
  // 强制右端点 updated：永远在最后一位、永远可见（端点对称设计 —— proj 在最左、updated 在最右，中间列才能拖动改变列宽）
  state.colOrder = [...state.colOrder.filter(k => k !== "updated"), "updated"];
  state.colVisible["updated"] = true;
  // 强制中间 auto 列（code / title / owner）一定是 auto —— 不管 localStorage 历史脏数据写的是什么。
  // 真正的可拖拽列只有 comments（中间固定列），其余中间列永远按内容自适应，不允许手动调宽。
  // 这样可以根治"任务负责人列可拖拽、遮挡文字"这类问题。
  for (const k of ["code", "title", "owner"]) {
    state.colModes[k] = "auto";
  }
  // 强制中间顺序（保持产品视角的阅读次序）：
  // 编号 → 名称 → 谁负责 → 描述 → 状态 → 开始时间 → 截止时间 → 逾期天数 → 评论，逻辑递进。
  // due / overdue 现在与普通看板共享（不再仅限延期视图），默认可见、可在「列设置」里关掉。
  {
    const middle = state.colOrder.filter(k => k !== "proj" && k !== "updated");
    const preferred = ["code", "title", "owner", "desc", "status", "start", "due", "overdue", "comments"];
    const seen = new Set();
    const ordered = [];
    for (const k of preferred){
      if (middle.includes(k) && !seen.has(k)){ ordered.push(k); seen.add(k); }
    }
    for (const k of middle){
      if (!seen.has(k)){ ordered.push(k); seen.add(k); }
    }
    state.colOrder = ["proj", ...ordered, "updated"];
  }
  // 响应式默认列：非宽屏（<1600px，即放不下全部 11 列）且用户从未保存过列配置时，
  // 默认隐藏低频列（描述 / 开始时间），把横向溢出从 ~490px 压到 ~130px；
  // 需要时可在「列设置」里重新打开（打开后配置会保存，之后不再受本规则影响）。
  if (!hadSaved && window.innerWidth < 1600){
    state.colVisible.desc = false;
    state.colVisible.start = false;
  }
}
function saveColumnConfig(){
  try {
    localStorage.setItem(COLUMN_CONFIG_KEY, JSON.stringify({
      order: state.colOrder,
      visible: state.colVisible,
      modes: state.colModes
    }));
  } catch(e){}
}
/** 弹窗变更后调用：持久化 + 重渲染当前页（不重新拉数据）。 */
function applyColumnConfig(){
  saveColumnConfig();
  // 重新触发渲染：用当前 items 渲染一次（性能可接受，因为评论都在 cache 里）
  if (state.lastItems) renderTable(state.lastItems);
}

// 把 state.colWidths 应用到当前页面所有 .proj-table 的 colgroup 上
//   - widths 索引是"原始下标"（renderTable 中 col 上的 data-col），与列顺序解耦
//   - 只有当 col 上声明了 data-col 时才覆盖，避免影响未受影响的列
function applyColumnWidths(){
  const widths = state.colWidths || {};
  const keys = Object.keys(widths);
  if (!keys.length) return;
  document.querySelectorAll("#tableArea .proj-table").forEach(tbl => {
    const cols = tbl.querySelectorAll("colgroup col");
    cols.forEach((c) => {
      const idx = parseInt(c.dataset.col, 10);
      if (Number.isFinite(idx) && widths[idx] != null){
        c.style.width = widths[idx] + "px";
      }
    });
  });
}

// 全局拖拽：仅绑定一次（页面生命周期内）
let _colDrag = null;

// 拖拽列宽的取值范围（bindColumnResize 用；列宽本身由 CSS auto 布局 + min-width 决定）
const DRAG_COL_MIN_W = 40;    // 拖拽列宽最小值（再小就看不见内容了）
const DRAG_COL_MAX_W = 600;   // 拖拽列宽最大值（防止用户拖出"无限制延伸"的不合理宽）
function bindColumnResize(){
  // 用事件委托：任何 col-resizer 的 mousedown 都接管（绑在 document 上，避免依赖元素是否存在）
  document.addEventListener("mousedown", e => {
    const hand = e.target.closest && e.target.closest(".col-resizer");
    if (!hand) return;
    e.preventDefault();
    const L_idx = parseInt(hand.dataset.col, 10);   // resizer 所属列 = 左侧列
    if (!Number.isFinite(L_idx)) return;
    const table = hand.closest("table");
    if (!table) return;

    // 在当前表里按 DOM 顺序找 L、R 两列的 <col> 节点
    const allCols = [...table.querySelectorAll("colgroup col")];
    const L_colNode = allCols.find(c => parseInt(c.dataset.col, 10) === L_idx);
    if (!L_colNode) return;
    const L_renderIdx = allCols.indexOf(L_colNode);
    const R_colNode = allCols[L_renderIdx + 1] || null;
    const R_idx = R_colNode ? parseInt(R_colNode.dataset.col, 10) : null;
    // R 是不是 auto 列？auto 不写 state.colWidths，但本次拖动期间需要临时调整 col.style.width。
    const R_isAuto = R_colNode ? (R_colNode.dataset.mode === "auto") : false;

    // 取"最窄 .proj-block 容器宽"作为总宽上限（跨表一致约束）
    const projBlock = table.parentElement;
    const allBlocks = document.querySelectorAll("#tableArea .proj-block");
    let minBlockW = Infinity;
    allBlocks.forEach(b => { if (b.clientWidth && b.clientWidth < minBlockW) minBlockW = b.clientWidth; });
    if (!isFinite(minBlockW)) minBlockW = projBlock.clientWidth || 1200;

    // L、R 之外的其他列宽之和
    let otherSum = 0;
    allCols.forEach(c => {
      const ci = parseInt(c.dataset.col, 10);
      if (ci === L_idx || ci === R_idx) return;
      let w = parseFloat(c.style.width);
      if (!w || !isFinite(w)) w = c.getBoundingClientRect().width || 0;
      otherSum += w;
    });

    // 起宽：先读 col.style.width（上次拖拽 save 或 colgroup 渲染写入），
    // 兜底读 getBoundingClientRect —— 后者在拖动过程中会被列和表格 layout 重算，
    // 这里只在 mousedown 抓取，所以是稳定的"起宽"。
    const startWL = (() => {
      let w = parseFloat(L_colNode.style.width);
      if (!w || !isFinite(w)) w = L_colNode.getBoundingClientRect().width || 0;
      return Math.round(w);
    })();
    let startWR = 0;
    if (R_colNode) {
      let w = parseFloat(R_colNode.style.width);
      if (!w || !isFinite(w)) w = R_colNode.getBoundingClientRect().width || 0;
      startWR = Math.round(w);
    }

    _colDrag = {
      L_idx, R_idx, R_isAuto, R_colNode,
      hand, table,
      startX: e.clientX,
      startWL, startWR,
      otherSum,
      maxWL: DRAG_COL_MAX_W,
      maxWR: DRAG_COL_MAX_W,
      minBlockW,
      lastW: { L: startWL, R: startWR }
    };
    hand.classList.add("dragging");
    document.body.classList.add("col-resizing");
  });

  document.addEventListener("mousemove", e => {
    if (!_colDrag) return;
    const dx = e.clientX - _colDrag.startX;
    const { L_idx, R_idx, R_isAuto, R_colNode, startWL, startWR, otherSum, maxWL, maxWR, minBlockW } = _colDrag;

    // 双列联动核心：L 加 dx、R 减 dx（总列宽不变 = startWL + startWR 不变）。
    let newWL = Math.round(startWL + dx);
    let newWR = Math.round(startWR - dx);

    // 1) 单列夹紧
    newWL = Math.max(DRAG_COL_MIN_W, Math.min(maxWL, newWL));
    if (R_colNode) newWR = Math.max(DRAG_COL_MIN_W, Math.min(maxWR, newWR));
    else newWR = 0;

    // 2) 容器上限：L+R 不得超出 minBlockW - otherSum
    const totalAllow = Math.max(
      DRAG_COL_MIN_W * (R_colNode ? 2 : 1),
      Math.floor(minBlockW - otherSum)
    );
    if (newWL + newWR > totalAllow){
      const over = newWL + newWR - totalAllow;
      // 优先让 R 列承担压缩（保留 L 的调整意愿）
      if (R_colNode){
        const shrink = Math.min(newWR - DRAG_COL_MIN_W, over);
        newWR -= shrink;
        const rest = over - shrink;
        if (rest > 0){
          newWL = Math.max(DRAG_COL_MIN_W, newWL - rest);
        }
      } else {
        newWL = Math.max(DRAG_COL_MIN_W, newWL - over);
      }
    }

    // 3) 与上次相同 → 跳过
    if (newWL === _colDrag.lastW.L && newWR === _colDrag.lastW.R) return;
    _colDrag.lastW = { L: newWL, R: newWR };

    // 4) 写 state（auto 列不持久化）
    state.colWidths[L_idx] = newWL;
    if (R_colNode && !R_isAuto){
      state.colWidths[R_idx] = newWR;
    }

    // 5) 同步所有表的对应 col.style.width
    document.querySelectorAll('#tableArea .proj-table colgroup col[data-col="' + L_idx + '"]')
      .forEach(c => { c.style.width = newWL + "px"; });
    if (R_colNode){
      document.querySelectorAll('#tableArea .proj-table colgroup col[data-col="' + R_idx + '"]')
        .forEach(c => { c.style.width = newWR + "px"; });
    }
    // 注：表格本身保持 CSS 的 width:100%（auto 布局），不再写死像素总宽；
    //     拖拽只作用于 col 的宽度提示，其余列由浏览器重新分配。
  });

  document.addEventListener("mouseup", () => {
    if (!_colDrag) return;
    _colDrag.hand.classList.remove("dragging");
    document.body.classList.remove("col-resizing");
    saveColumnWidths();
    _colDrag = null;
  });
}

function show(el){ document.getElementById(el).classList.remove("hidden"); }
function hide(el){ document.getElementById(el).classList.add("hidden"); }

async function api(path, opts){
  const resp = await fetch(path, Object.assign({credentials:"same-origin"}, opts));
  let data = null;
  try { data = await resp.json(); } catch(e){}
  if (resp.status === 401){
    // 会话失效，回到登录页
    hide("boardView"); show("loginView");
    throw new Error("会话已失效，请重新登录");
  }
  if (!resp.ok || (data && data.ok === false)){
    throw new Error((data && data.error) || ("请求失败：" + resp.status));
  }
  return data;
}

async function doLogin(){
  const btn = document.getElementById("loginBtn");
  const alert = document.getElementById("loginAlert");
  alert.classList.add("hidden");
  const clientId = document.getElementById("clientId").value.trim();
  const clientSecret = document.getElementById("clientSecret").value.trim();
  const tenant = document.getElementById("tenantInput").value.trim();
  if (!clientId || !clientSecret){
    alert.textContent = "Client ID 和 Client Secret 均不能为空";
    alert.classList.remove("hidden");
    return;
  }
  if (!tenant){
    alert.textContent = "Worktile 域名不能为空（用于跳转任务详情页）";
    alert.classList.remove("hidden");
    return;
  }
  btn.disabled = true; btn.textContent = "验证中…";
  try {
    const r = await api("/api/login", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        client_id: clientId,
        client_secret: clientSecret,
        tenant: tenant
      })
    });
    if (r.tenant_host) state.tenantHost = r.tenant_host;
    if (r.client_id_masked) state.clientIdMasked = r.client_id_masked;
    if (r.tenant_fingerprint) state.tenantFingerprint = r.tenant_fingerprint;
    renderTenantBadge();
    hide("loginView"); show("boardView");
    await loadProjects();
  } catch(e){
    alert.textContent = e.message;
    alert.classList.remove("hidden");
  } finally {
    btn.disabled = false; btn.textContent = "进入看板";
  }
}

async function doLogout(){
  try { await api("/api/logout", {method:"POST"}); } catch(e){}
  state.expanded.clear(); state.commentsCache = {}; state.commentsExpanded = new Set();
  state.clientIdMasked = ""; state.tenantFingerprint = "";
  renderTenantBadge();
  hide("boardView"); show("loginView");
  document.getElementById("clientSecret").value = "";
}

/** 顶栏租户徽标：展示当前租户打码 client_id 与指纹，让用户直观确认
 *  自己处于隔离的租户空间（与其他租户数据互不可见）。 */
function renderTenantBadge(){
  const el = document.getElementById("tenantBadge");
  if (!el) return;
  if (state.clientIdMasked){
    const fp = state.tenantFingerprint ? ` · #${state.tenantFingerprint}` : "";
    el.textContent = `${state.clientIdMasked}${fp}`;
    el.classList.add("active");
  } else {
    el.textContent = "未登录";
    el.classList.remove("active");
  }
}

async function loadProjects(){
  const data = await api("/api/projects");
  state.projects = data.projects || [];  renderProjectMenu("");
  if (data.projects && data.projects.length){
    selectProject("__all__", "全部项目");
    // 项目列表拉取成功后，并行拉负责人列表（独立接口，失败不影响主流程）
    loadMembers().catch(e => console.warn("loadMembers 失败：", e && e.message));
  } else if (data.error){
    document.getElementById("tableArea").innerHTML =
      '<div class="loading error">加载项目失败：' + escapeHtml(data.error) +
      '<div class="error-hint">请检查网络代理是否能访问 Worktile 服务器（沙箱出网代理可能会拦截）。</div></div>';
  } else {
    document.getElementById("tableArea").innerHTML =
      '<div class="empty">该租户下没有可用的项目</div>';
  }
}

/** 从 /api/members 拉所有成员并写到 state.members，刷新负责人下拉。
 * 失败抛错（由调用方选择忽略或提示），不阻塞主流程。 */
async function loadMembers(){
  const data = await api("/api/members");
  state.members = data.members || [];
  renderOwnerMenu("");
}

function selectProject(id, name){
  state.projectId = id;
  state.projectName = name;
  // 切换项目时清空负责人筛选，避免跨项目残留不同成员造成"筛了等于没筛"的困惑
  state.ownerFilter = null;
  updateOwnerInput();
  closeProjectMenu();
  // 切换项目时清空任务名称搜索（chips + 草稿），避免跨项目残留关键字造成困惑
  // clearTitleSearch 内部会重置分页与缓存并 reload，无需下面重复
  clearTitleSearch();
  // 切换项目后立即清空「延期任务」按钮上的数字（避免残留上次的项目数误导用户），
  // 然后触发一次轻量探测刷新 badge：
  //   - 单项目：调 /api/tasks/overdue?project_id=...&count_only=1，只数 total 不返回 items，
  //             1 秒内能拿到正确数字
  //   - 全部项目（__all__）：跳过探测（要扫 89 个项目、~13s，主动探测会卡 UI），
  //             保持按钮无数字，等用户主动点「延期任务」按钮时再扫描
  const lbl = document.getElementById("overdueBtnLabel");
  if (lbl){
    lbl.textContent = "延期任务";
    lbl.classList.remove("scanning");
  }
  if (id && id !== "__all__") refreshOverdueBadge(id);
}

/** 轻量探测指定项目（可叠加负责人过滤）的延期任务数，仅用于刷新顶部 badge。
 * 不传 project_id 或 id=="__all__" 时直接跳过（避免全项目扫描卡 UI）。
 * owner_uid：
 *   - null / "" / undefined → 不过滤（项目管理下的全部延期数）
 *   - "__unassigned__"     → 后端按 _assignee_uid 为空过滤
 *   - 其他字符串            → 后端按 _assignee_uid 精确匹配 */
async function refreshOverdueBadge(projectId, ownerUid){
  if (!projectId || projectId === "__all__") return;
  const lbl = document.getElementById("overdueBtnLabel");
  if (!lbl) return;
  lbl.textContent = "延期任务 ···";
  lbl.classList.add("scanning");
  let url = `/api/tasks/overdue?project_id=${encodeURIComponent(projectId)}&count_only=1`;
  if (ownerUid) url += `&owner=${encodeURIComponent(ownerUid)}`;
  try {
    const data = await api(url);
    // 用户可能在探测期间又切了项目/负责人 → 只在 state 仍匹配时回写
    if (state.projectId === projectId){
      const cur = state.ownerFilter;
      const curUid = cur === "__unassigned__" ? "__unassigned__"
                   : (cur && cur.uid) || null;
      // owner 也匹配（同一 owner 才能回写，避免"切了 owner 但旧响应覆盖"）。
      // 用 == 而非 ===：selectProject 探测时 ownerUid=undefined、curUid=null，
      // 两者都视作"未指定 owner"，应允许回写数字。
      if (curUid == ownerUid && data && data.ok && data.total != null){
        lbl.textContent = `延期任务 (${data.total})`;
      } else {
        lbl.textContent = "延期任务";
      }
    } else {
      lbl.textContent = "延期任务";
    }
  } catch(e){
    lbl.textContent = "延期任务";
  } finally {
    lbl.classList.remove("scanning");
  }
}

/* ====================== 搜索式项目选择器 ====================== */
let _pickerHighlighted = -1;

function initProjectPicker(){
  const input = document.getElementById("projectInput");
  const menu = document.getElementById("projectMenu");
  const picker = document.getElementById("projectPicker");
  input.addEventListener("focus", () => {
    input.select();
    openProjectMenu();
  });
  input.addEventListener("input", () => renderProjectMenu(input.value));
  input.addEventListener("keydown", onProjectKey);
  // 点击外部自动关闭
  document.addEventListener("click", (e) => {
    if (!picker.contains(e.target)) closeProjectMenu();
  });
}

/** 把 fixed 定位的浮层对齐到 anchor 元素下方（与 input 同宽）。 */
function positionMenuUnder(menu, anchor){
  if (!menu || !anchor) return;
  const r = anchor.getBoundingClientRect();
  menu.style.minWidth = r.width + "px";
  menu.style.left = r.left + "px";
  menu.style.top  = (r.bottom + 4) + "px";
}

function openProjectMenu(){
  const input = document.getElementById("projectInput");
  const menu = document.getElementById("projectMenu");
  // 若输入框当前显示的是已选项名，重新聚焦打开时不预填筛选
  renderProjectMenu(input.value === state.projectName ? "" : input.value);
  positionMenuUnder(menu, input);
  menu.hidden = false;
  _pickerHighlighted = -1;
}

function closeProjectMenu(){
  document.getElementById("projectMenu").hidden = true;
  document.getElementById("projectInput").value = state.projectName || "";
  _pickerHighlighted = -1;
}

function renderProjectMenu(filterText){
  const menu = document.getElementById("projectMenu");
  menu.innerHTML = "";
  const items = [{id:"__all__", name:"全部项目"}]
    .concat((state.projects || []).map(p => ({id: p.id, name: p.name})));
  const q = (filterText || "").trim().toLowerCase();
  const filtered = q
    ? items.filter(it => it.name.toLowerCase().includes(q))
    : items;
  if (!filtered.length){
    const empty = document.createElement("div");
    empty.className = "opt-empty";
    empty.textContent = "没有匹配的项目";
    menu.appendChild(empty);
    return;
  }
  filtered.forEach((it, idx) => {
    const div = document.createElement("div");
    div.className = "opt" + (it.id === state.projectId ? " selected" : "");
    div.dataset.id = it.id;
    if (q){
      const lower = it.name.toLowerCase();
      const pos = lower.indexOf(q);
      if (pos >= 0){
        div.innerHTML =
          escapeHtml(it.name.slice(0, pos)) +
          '<span class="hl">' + escapeHtml(it.name.slice(pos, pos + q.length)) + '</span>' +
          escapeHtml(it.name.slice(pos + q.length));
      } else {
        div.textContent = it.name;
      }
    } else {
      div.textContent = it.name;
    }
    div.addEventListener("click", () => selectProject(it.id, it.name));
    div.addEventListener("mouseenter", () => {
      _pickerHighlighted = idx;
      updatePickerHighlight();
    });
    menu.appendChild(div);
  });
}

function updatePickerHighlight(){
  const items = document.querySelectorAll("#projectMenu .opt");
  items.forEach((el, i) => el.classList.toggle("highlighted", i === _pickerHighlighted));
}

function scrollHighlightedIntoView(){
  const menu = document.getElementById("projectMenu");
  const el = menu.querySelector(".opt.highlighted");
  if (el) el.scrollIntoView({block:"nearest"});
}

function onProjectKey(e){
  const menu = document.getElementById("projectMenu");
  const input = document.getElementById("projectInput");
  if (e.key === "Escape"){
    closeProjectMenu();
    input.blur();
    e.preventDefault();
    return;
  }
  const items = Array.from(document.querySelectorAll("#projectMenu .opt"));
  if (e.key === "ArrowDown"){
    if (menu.hidden) openProjectMenu();
    _pickerHighlighted = Math.min(_pickerHighlighted + 1, Math.max(items.length - 1, 0));
    updatePickerHighlight();
    scrollHighlightedIntoView();
    e.preventDefault();
  } else if (e.key === "ArrowUp"){
    _pickerHighlighted = Math.max(_pickerHighlighted - 1, 0);
    updatePickerHighlight();
    scrollHighlightedIntoView();
    e.preventDefault();
  } else if (e.key === "Enter"){
    if (!menu.hidden && _pickerHighlighted >= 0 && items[_pickerHighlighted]){
      const it = items[_pickerHighlighted];
      selectProject(it.dataset.id, it.textContent.trim());
    }
    e.preventDefault();
  }
}

/* ====================== 搜索式负责人选择器 ======================
 * 复用 projectPicker 的 search-select 样式与下拉浮层，独立维护一份高亮下标。
 * 选项分两组：
 *   1) 顶部固定两项："全部负责人"（uid=null）、"未分配"（uid="__unassigned__"）
 *   2) 下面按 name 排序的全部租户成员
 * 选择某项后：state.ownerFilter = null / "__unassigned__" / {uid, name}，
 * 然后 reload 当前视图（重置分页到第 0 页，复用 loadTasks）。 */
let _ownerHighlighted = -1;

function initOwnerPicker(){
  const input = document.getElementById("ownerInput");
  const menu = document.getElementById("ownerMenu");
  const picker = document.getElementById("ownerPicker");
  input.addEventListener("focus", () => {
    input.select();
    openOwnerMenu();
  });
  input.addEventListener("input", () => renderOwnerMenu(input.value));
  input.addEventListener("keydown", onOwnerKey);
  document.addEventListener("click", (e) => {
    if (!picker.contains(e.target)) closeOwnerMenu();
  });
}

function openOwnerMenu(){
  const input = document.getElementById("ownerInput");
  const menu = document.getElementById("ownerMenu");
  renderOwnerMenu(input.value === ownerFilterLabel() ? "" : input.value);
  positionMenuUnder(menu, input);
  menu.hidden = false;
  _ownerHighlighted = -1;
}

function closeOwnerMenu(){
  const menu = document.getElementById("ownerMenu");
  if (menu) menu.hidden = true;
  updateOwnerInput();
  _ownerHighlighted = -1;
}

/** 把顶部负责人输入框的显示文本与 state.ownerFilter 同步。 */
function updateOwnerInput(){
  const input = document.getElementById("ownerInput");
  if (input) input.value = ownerFilterLabel();
}

/** 当前负责人筛选条件的展示文本（用于 input 回填与下拉"selected"判定） */
function ownerFilterLabel(){
  const f = state.ownerFilter;
  if (f == null) return "全部负责人";
  if (f === "__unassigned__") return "未分配";
  return f.name || f.uid;
}

function renderOwnerMenu(filterText){
  const menu = document.getElementById("ownerMenu");
  if (!menu) return;
  menu.innerHTML = "";
  // 顶部固定项（特殊语义）
  const fixed = [
    { uid: null, name: "全部负责人" },
    { uid: "__unassigned__", name: "未分配" },
  ];
  const members = state.members || [];
  const all = fixed.concat(members);
  const q = (filterText || "").trim().toLowerCase();
  const filtered = q
    ? all.filter(it => it.name.toLowerCase().includes(q))
    : all;
  if (!filtered.length){
    const empty = document.createElement("div");
    empty.className = "opt-empty";
    empty.textContent = "没有匹配的负责人";
    menu.appendChild(empty);
    return;
  }
  const currentLabel = ownerFilterLabel();
  filtered.forEach((it, idx) => {
    const div = document.createElement("div");
    div.className = "opt" + (it.name === currentLabel ? " selected" : "");
    div.dataset.uid = it.uid == null ? "" : String(it.uid);
    div.dataset.unassigned = it.uid === "__unassigned__" ? "1" : "0";
    if (q){
      const lower = it.name.toLowerCase();
      const pos = lower.indexOf(q);
      if (pos >= 0){
        div.innerHTML =
          escapeHtml(it.name.slice(0, pos)) +
          '<span class="hl">' + escapeHtml(it.name.slice(pos, pos + q.length)) + '</span>' +
          escapeHtml(it.name.slice(pos + q.length));
      } else {
        div.textContent = it.name;
      }
    } else {
      div.textContent = it.name;
    }
    div.addEventListener("click", () => {
      let f;
      if (it.uid == null) f = null;
      else if (it.uid === "__unassigned__") f = "__unassigned__";
      else f = { uid: it.uid, name: it.name };
      selectOwner(f);
    });
    div.addEventListener("mouseenter", () => {
      _ownerHighlighted = idx;
      updateOwnerHighlight();
    });
    menu.appendChild(div);
  });
}

function updateOwnerHighlight(){
  const items = document.querySelectorAll("#ownerMenu .opt");
  items.forEach((el, i) => el.classList.toggle("highlighted", i === _ownerHighlighted));
}

function onOwnerKey(e){
  const menu = document.getElementById("ownerMenu");
  const input = document.getElementById("ownerInput");
  if (e.key === "Escape"){
    closeOwnerMenu();
    input.blur();
    e.preventDefault();
    return;
  }
  const items = Array.from(document.querySelectorAll("#ownerMenu .opt"));
  if (e.key === "ArrowDown"){
    if (menu.hidden) openOwnerMenu();
    _ownerHighlighted = Math.min(_ownerHighlighted + 1, Math.max(items.length - 1, 0));
    updateOwnerHighlight();
    scrollOwnerHighlightedIntoView();
    e.preventDefault();
  } else if (e.key === "ArrowUp"){
    _ownerHighlighted = Math.max(_ownerHighlighted - 1, 0);
    updateOwnerHighlight();
    scrollOwnerHighlightedIntoView();
    e.preventDefault();
  } else if (e.key === "Enter"){
    if (!menu.hidden && _ownerHighlighted >= 0 && items[_ownerHighlighted]){
      const it = items[_ownerHighlighted];
      const isUnassigned = it.dataset.unassigned === "1";
      const uid = it.dataset.uid;
      let f;
      if (!uid) f = null;
      else if (isUnassigned) f = "__unassigned__";
      else f = { uid: uid, name: it.textContent.trim() };
      selectOwner(f);
    }
    e.preventDefault();
  }
}

function scrollOwnerHighlightedIntoView(){
  const menu = document.getElementById("ownerMenu");
  const el = menu.querySelector(".opt.highlighted");
  if (el) el.scrollIntoView({block:"nearest"});
}

/** 选了一个负责人（包含特殊项 null / "__unassigned__"），更新 state + UI + 重新加载。
 * 与项目切换不同：选负责人不重置页码到 0（如果已经在第 2 页，应该继续在第 2 页过滤），
 * 但要清空评论缓存和折叠状态（因为表格里任务列表会变）。 */
function selectOwner(filter){
  state.ownerFilter = filter;
  closeOwnerMenu();
  state.expanded.clear();
  state.commentsCache = {};
  // 当前视图如果是 overdue，服务端已按 project_id 全量返回；前端只做内存过滤
  // board 视图同理：分页是按"原始 items"分页的，过滤在拿到 items 之后做
  loadTasks();
  // board 视图下 loadTasks 走 /api/tasks（不带 overdue badge 刷新）；
  // 这里手动触发一次轻量探测，让 badge 数字跟着 owner 走（与表格完全一致），
  //   owner_filter: __all__（全部） → 跳过（要扫全部项目，~~慢）
  //   owner_filter: __unassigned__ / 具体 uid → 按 owner 精确计数
  // 注：overdue 视图下 loadTasks 内部已经会更新 badge，不需要重复
  if (state.view !== "overdue" && state.projectId && state.projectId !== "__all__"){
    const ownerParam = state.ownerFilter === "__unassigned__"
      ? "__unassigned__"
      : (state.ownerFilter && state.ownerFilter.uid) || null;
    refreshOverdueBadge(state.projectId, ownerParam);
  }
}

/** 判定一条任务是否通过当前负责人筛选，返回 true=通过 / false=不通过。
 * 任务 assignee 字段是 display_name（"未分配" / "John" 等），uid 不会暴露给前端。
 * 三种 state.ownerFilter：
 *   - null            → 所有任务都通过
 *   - "__unassigned__"→ 仅 assignee === "未分配"
 *   - {uid, name}     → 仅 assignee === name（用 display_name 匹配，uid 仅供后端/审计，
 *                       不下发给前端，避免暴露内部 id 形态）
 */
function matchesOwnerFilter(task){
  const f = state.ownerFilter;
  if (f == null) return true;
  if (f === "__unassigned__") return !task.assignee || task.assignee === "未分配";
  return task.assignee && task.assignee === f.name;
}

function onPageSizeChange(){
  state.pageSize = parseInt(document.getElementById("pageSize").value, 10);
  state.page = 0; state.expanded.clear(); state.commentsCache = {}; state.commentsExpanded = new Set();
  loadTasks();
}

function refreshAll(){
  // 「刷新」按钮语义：强制重新拉取所有数据（包括评论缓存）。
  // 否则用户在 Worktile 后台改了评论、点刷新按钮时，评论列表依旧命中
  // state.commentsCache，看不到新评论。
  const btn = document.getElementById("refreshBtn");
  if (btn.disabled) return;
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = "刷新中…";
  state.expanded.clear();
  state.commentsCache = {};
  if (state.view === "overdue") state.overdueForceRefresh = true;  // 逾期视图：强制重新扫描全部项目
  loadTasks().finally(() => {
    // 等 loadTasks 完成（resolve 或 reject）后恢复按钮
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = oldText;
    }, 400);
  });
}

async function loadTasks(){
  if (!state.projectId) return;
  const area = document.getElementById("tableArea");
  const isOverdue = state.view === "overdue";
  // 逾期视图根据当前 projectId 显示不同 loading 文案，让用户清楚扫描范围
  let loadingHtml;
  if (isOverdue){
    const isAll = state.projectId === "__all__";
    loadingHtml = isAll
      ? '<div class="loading">正在扫描全部项目的延期任务，请稍候…</div>'
      : `<div class="loading">正在扫描「${escapeHtml(state.projectName)}」的延期任务，请稍候…</div>`;
  } else {
    loadingHtml = '<div class="loading">加载任务中…</div>';
  }
  area.innerHTML = loadingHtml;
  try {
    // 延期视图：忽略关键字搜索，直接拉 /api/tasks/overdue（服务端已按范围全量过滤+缓存）
    const kw = isOverdue ? "" : buildSearchKeyword();
    const base = isOverdue ? "/api/tasks/overdue" : "/api/tasks";
    let url = `${base}?project_id=${encodeURIComponent(state.projectId)}`
      + `&page=${state.page}&page_size=${state.pageSize}`;
    if (isOverdue && state.overdueForceRefresh){
      url += "&refresh=1";
      state.overdueForceRefresh = false;
    } else if (kw){
      url += `&keyword=${encodeURIComponent(kw)}`;
    }
    // 延期视图透传 owner 过滤给后端（uid 形态，wt_client.normalize_task 已 _ 前缀化）
    // - badge 上的数字 = data.total，必须是后端按 owner 过滤后的全集数
    //   而不是前端的 items.length（分页模式下 items 只是一页）
    // - 后端会按 (project_id, owner_filter) 二元 key 缓存，不同 owner 互不污染
    if (isOverdue && state.ownerFilter){
      const ownerParam = state.ownerFilter === "__unassigned__"
        ? "__unassigned__"
        : (state.ownerFilter.uid || "");
      if (ownerParam) url += `&owner=${encodeURIComponent(ownerParam)}`;
    }
    const data = await api(url);
    state.total = data.total;
    state.hasMore = data.has_more;
    state.lastItems = data.items || [];     // 缓存一份，供「列设置变更后本地重渲」使用
    // 逾期视图：用后端返回的真实 project_name 同步顶部项目筛选框，
    // 避免用户在 overdue 视图时顶部还停留在旧值
    if (isOverdue && data.project_name){
      state.projectName = data.project_name;
      const inputEl = document.getElementById("projectInput");
      if (inputEl) inputEl.value = state.projectName;
    }
    renderTable(state.lastItems);
    renderPager();
    if (isOverdue){
      const lbl = document.getElementById("overdueBtnLabel");
      if (lbl) lbl.textContent = `延期任务 (${data.total != null ? data.total : state.lastItems.length})`;
      if (lbl) lbl.classList.remove("scanning");
    }
  } catch(e){
    area.innerHTML = `<div class="alert">${escapeHtml(e.message)}</div>`;
  }
}

// ====================== 一键导出 Excel ======================
/** 切换导出模式：仅任务 / 含评论（默认含评论） */
function setExportMode(mode){
  state.exportMode = mode;
  const t = document.getElementById("expTasks");
  const c = document.getElementById("expComments");
  if (t) t.classList.toggle("active", mode === "tasks");
  if (c) c.classList.toggle("active", mode === "comments");
}

/** 按当前视图 + 筛选条件导出 Excel（服务端全量拉取，复用鉴权会话）。 */
async function exportExcel(){
  if (!state.projectId){ alert("请先选择项目"); return; }
  const btn = document.getElementById("exportBtn");
  if (_exportJobId){ return; }  // 已有导出进行中，忽略重复点击

  const isOverdue = state.view === "overdue";
  const body = {
    view: isOverdue ? "overdue" : "board",
    project_id: state.projectId,
    with_comments: state.exportMode === "comments",
  };
  // 负责人筛选对两个视图都传给后端（board/overdue 后端均支持 owner 过滤）。
  // 之前只在 overdue 视图传，board 视图导出会包含全部负责人的数据，与页面所见不符。
  if (state.ownerFilter){
    const ownerParam = state.ownerFilter === "__unassigned__"
      ? "__unassigned__" : (state.ownerFilter.uid || "");
    if (ownerParam) body.owner = ownerParam;
  }
  if (!isOverdue){
    const kw = buildSearchKeyword();
    if (kw) body.keyword = kw;
  }

  btn.disabled = true;
  openExportProgress();
  try {
    const r = await fetch("/api/export/start", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    if (r.status === 401){
      hide("boardView"); show("loginView");
      throw new Error("会话已失效，请重新登录");
    }
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "启动导出失败");
    _exportJobId = d.job_id;
    await _pollExport();
  } catch(e){
    closeExportProgress();
    alert(e.message || "导出出错");
    btn.disabled = false;
    _exportJobId = null;
  }
}

// 导出任务轮询：拿到进度后更新浮层，done 时下载文件
let _exportJobId = null;

async function _pollExport(){
  const btn = document.getElementById("exportBtn");
  while (_exportJobId){
    let d;
    try {
      const r = await fetch(`/api/export/progress?job_id=${_exportJobId}`, {credentials: "same-origin"});
      if (r.status === 401){
        hide("boardView"); show("loginView");
        throw new Error("会话已失效，请重新登录");
      }
      if (!r.ok) throw new Error("进度查询失败");
      d = await r.json();
    } catch(e){
      throw new Error(e.message || "进度查询失败");
    }
    if (!d.ok) throw new Error(d.error || "导出失败");

    updateExportProgress(d);

    if (d.status === "done"){
      try {
        const dl = await fetch(`/api/export/download?job_id=${_exportJobId}`, {credentials: "same-origin"});
        if (!dl.ok){
          let msg = "文件下载失败";
          try { const dd = await dl.json(); if (dd && dd.error) msg = dd.error; } catch(e){}
          throw new Error(msg);
        }
        const blob = await dl.blob();
        const fname = d.filename || "worktile_export.xlsx";
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      } finally {
        closeExportProgress();
        btn.disabled = false;
        _exportJobId = null;
      }
      return;
    }
    if (d.status === "error"){
      throw new Error(d.error || "导出失败");
    }
    await new Promise(res => setTimeout(res, 600));
  }
}

function openExportProgress(){
  const fill = document.getElementById("epFill");
  fill.style.width = "0%";
  document.getElementById("epStatus").textContent = "准备中…";
  document.getElementById("epMeta").textContent = "";
  show("exportProgress");
}

function closeExportProgress(){
  hide("exportProgress");
}

function updateExportProgress(d){
  const fill = document.getElementById("epFill");
  const status = document.getElementById("epStatus");
  const meta = document.getElementById("epMeta");

  let pct = 0, label = "", detail = "";
  if (d.phase === "tasks"){
    pct = 5;
    label = "收集任务…";
    detail = d.task_count ? `已收集 ${d.task_count} 条` : "正在遍历任务列表";
  } else if (d.phase === "comments"){
    const total = d.comment_total || 0;
    const done = d.comment_done || 0;
    pct = total ? Math.round(10 + 80 * (done / total)) : 10;
    label = "拉取评论…";
    detail = total ? `${done}/${total} 个任务` : "正在并发拉取评论";
  } else if (d.phase === "build"){
    pct = 95;
    label = "生成 Excel 文件…";
    detail = "";
  } else if (d.status === "done"){
    pct = 100;
    label = "完成";
    detail = `${d.task_count} 个任务` + (d.comment_count ? ` · ${d.comment_count} 条评论` : "");
  }
  fill.style.width = pct + "%";
  status.textContent = label;
  meta.textContent = detail;
}

// ====================== 延期任务视图切换 ======================
// 逾期视图使用独立的列布局（含 截止时间 / 逾期天数），与主看板布局互不污染
const OVERDUE_ORDER = ["proj", "code", "title", "desc", "status", "start", "owner", "due", "overdue", "comments", "updated"];
const OVERDUE_VISIBLE = Object.fromEntries(OVERDUE_ORDER.map(k => [k, true]));
// proj 列固定 180px（避免长项目名撑宽整表；与用户保存的自定义宽度一致）；
// 其他列保持 auto（按内容自适应）；只有 comments 是 fixed（中间可拖拽列）。
const OVERDUE_MODES = Object.fromEntries(
  OVERDUE_ORDER.map(k => {
    if (k === "comments" || k === "proj") return [k, "fixed"];
    return [k, "auto"];
  }));

/** 点击工具栏「延期任务」按钮：在 board / overdue 两视图间切换 */
function toggleOverdueView(){
  setView(state.view === "overdue" ? "board" : "overdue");
}

/** 切换视图并加载对应数据。board 恢复用户保存的主看板布局；overdue 用独立列布局。 */
function setView(v){
  if (v === "overdue"){
    state.view = "overdue";
    state.colOrder = OVERDUE_ORDER.slice();
    state.colVisible = { ...OVERDUE_VISIBLE };
    state.colModes = { ...OVERDUE_MODES };
    // 不再硬覆盖 state.projectName：让顶部项目框如实反映当前 projectId 的范围
    // （loadTasks 收到响应后再用后端真实 project_name 同步一次）
  } else {
    state.view = "board";
    if (state.boardColOrder) state.colOrder = state.boardColOrder.slice();
    if (state.boardColVisible) state.colVisible = { ...state.boardColVisible };
    if (state.boardColModes) state.colModes = { ...state.boardColModes };
  }
  state.page = 0;
  updateViewUI();
  loadTasks();
}

/** 视图切换后更新工具栏按钮高亮状态 */
function updateViewUI(){
  const btn = document.getElementById("overdueBtn");
  if (btn) btn.classList.toggle("active", state.view === "overdue");
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}

/* 把任意字符串稳定地映射到 1~6 的色板索引（用于项目色点 / 负责人头像取色） */
function colorIndexFromName(name){
  let h = 0;
  const s = String(name || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return (h % 6) + 1;
}
function projColorClass(name){ return "proj-dot-c" + colorIndexFromName(name); }
function ownerColorClass(name){ return "c" + colorIndexFromName(name); }

/**
 * 拼接 Worktile 任务详情页 URL。
 * 规则（已用真实 URL 验证）：https://{tenant}/mission/projects/{project_id}/tasks/{task_id}
 * 缺少租户域名或 project_id/task_id 时返回 null（调用方降级为纯文本）。
 */
function buildTaskUrl(t){
  if (!state.tenantHost || !t.project_id || !t.task_id) return null;
  return "https://" + state.tenantHost
    + "/mission/projects/" + encodeURIComponent(t.project_id)
    + "/tasks/" + encodeURIComponent(t.task_id);
}

/** 任务名称渲染：可点击 → 新标签页打开 Worktile 任务详情；缺字段时降级为纯文本。
 *  a 的 title 显示完整任务名（不再是"在新标签页打开..."），便于多行换行/截断时 hover 看到全名。 */
function buildTaskTitle(t){
  const url = buildTaskUrl(t);
  const titleHtml = highlightTitle(t.title || "");
  if (!url) return titleHtml;
  const fullTitle = (t.title || "").trim();
  return `<a href="${url}" target="_blank" rel="noopener" `
    + `title="${escapeHtml(fullTitle)}">${titleHtml}</a>`;
}

/** 任务名称命中多个关键字（按 | 拆分）的部分加黄色高亮（先转义再包裹，避免 XSS/错位）。 */
function highlightTitle(title){
  const kw = (buildSearchKeyword() || "").trim().toLowerCase();
  if (!kw) return escapeHtml(title);
  const terms = kw.split("|").map(s => s.trim()).filter(Boolean);
  if (!terms.length) return escapeHtml(title);
  const lower = (title || "").toLowerCase();
  // 标记所有命中区间（按 | 拆分，任一匹配即高亮），然后按位置排序+合并重叠段
  const ranges = [];
  for (const t of terms){
    let i = 0, idx;
    while ((idx = lower.indexOf(t, i)) !== -1){
      ranges.push([idx, idx + t.length]);
      i = idx + t.length;
    }
  }
  if (!ranges.length) return escapeHtml(title);
  ranges.sort((a,b) => a[0]-b[0] || b[1]-a[1]);
  const merged = [];
  for (const r of ranges){
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  let out = "", i = 0;
  for (const [s, e] of merged){
    out += escapeHtml(title.slice(i, s));
    out += '<span class="hl">' + escapeHtml(title.slice(s, e)) + '</span>';
    i = e;
  }
  out += escapeHtml(title.slice(i));
  return out;
}

// ====================== 表格渲染（board / overdue 两个视图共用列渲染） ======================

/** 当前可见列（state.colOrder + state.colVisible 派生）；全隐藏时兜底保留「任务编号」列。 */
function computeVisibleCols(){
  const cols = state.colOrder
    .map((key, originalIdx) => {
      const meta = COLS_META.find(m => m.key === key);
      return { key, label: meta.label, anchor: !!meta.anchor, endAnchor: !!meta.endAnchor, originalIdx };
    })
    .filter(c => state.colVisible[c.key]);
  if (!cols.length){
    cols.push({ key: "code", label: "任务编号", anchor: false, originalIdx: 1 });
    state.colVisible.code = true;
  }
  return cols;
}

/** 列单元格公共属性：auto 列标 data-mode（CSS 据此 nowrap 贴内容），fixed 列写内联 min/max-width。
 *  固定列必须写内联宽，否则 table-layout 下浏览器按内容把 fixed 列也压成内容宽。 */
function cellModeAttrs(c){
  const dataMode = state.colModes[c.key] === "auto" ? ' data-mode="auto"' : '';
  const fixedW = state.colWidths[c.originalIdx] || DEFAULT_COL_WIDTHS[c.key];
  const fixStyle = state.colModes[c.key] === "fixed"
    ? ` style="min-width:${fixedW}px; max-width:${fixedW}px"`
    : '';
  return dataMode + fixStyle;
}

/** 生成 colgroup：auto 列不写宽（按内容自适应），fixed 列写显式宽。
 *  data-col 存「原始下标」，与 state.colWidths 索引对齐——用户拖宽后重排列顺序也不丢宽度。
 *  projWidth：proj 列默认宽（board 用 200，overdue 用 150）。 */
function renderColgroupHtml(visibleCols, projWidth){
  let html = '<colgroup>';
  visibleCols.forEach((c) => {
    if (state.colModes[c.key] === "auto"){
      html += `<col data-col="${c.originalIdx}" data-mode="auto">`;
    } else {
      const defW = c.key === "proj" ? projWidth : DEFAULT_COL_WIDTHS[c.key];
      const w = state.colWidths[c.originalIdx] || defW;
      html += `<col data-col="${c.originalIdx}" data-mode="fixed" style="width:${w}px">`;
    }
  });
  return html + '</colgroup>';
}

/** 生成 thead：只有「评论」是真正的中间固定列（渲染拖拽手柄）；
 *  端点列标 data-endanchor（CSS 据此右贴最右）。 */
function renderTheadHtml(visibleCols){
  let html = '<thead><tr>';
  visibleCols.forEach((c, renderIdx) => {
    const isLast = renderIdx === visibleCols.length - 1;
    const isAnchor = !!c.anchor || !!c.endAnchor;
    const isMiddleFixed = !isLast && !isAnchor && c.key === "comments";
    const resizer = isMiddleFixed
      ? `<span class="col-resizer" data-col="${c.originalIdx}"></span>`
      : '';
    const thAttr = (state.colModes[c.key] === "auto" ? ' data-mode="auto"' : '')
      + (c.endAnchor ? ' data-endanchor="1"' : '');
    html += `<th${thAttr}>${resizer}${escapeHtml(c.label)}</th>`;
  });
  return html + '</tr></thead>';
}

/** 评论列单元格内容（加载中 / 空 / 已缓存三态；缓存命中默认折叠只显最近几条）。 */
const COMMENTS_PREVIEW = 2;   // 折叠态默认展示的最近评论条数
function commentsCellHtml(taskId){
  const cached = state.commentsCache[taskId];
  if (cached === undefined) return '<div class="cell-loading">加载评论中…</div>';
  if (!cached.length) return '<div class="cell-empty">暂无评论</div>';
  const expanded = state.commentsExpanded.has(taskId);
  const shown = expanded ? cached : cached.slice(0, COMMENTS_PREVIEW);
  let html = '<div class="comments-inner">' + shown.map(commentHtml).join("") + '</div>';
  if (cached.length > COMMENTS_PREVIEW){
    html += `<button class="cmt-toggle" onclick="toggleCommentsExpand('${escapeHtml(taskId)}')">`
      + (expanded ? "收起评论" : `展开全部 ${cached.length} 条评论`)
      + '</button>';
  }
  return html;
}

/** 展开 / 收起某任务的全部评论（只重渲染该单元格，不重拉数据）。 */
function toggleCommentsExpand(taskId){
  if (state.commentsExpanded.has(taskId)) state.commentsExpanded.delete(taskId);
  else state.commentsExpanded.add(taskId);
  const cell = document.querySelector(`td.cell-comments[data-comments="${cssEscape(taskId)}"]`);
  if (cell) cell.innerHTML = commentsCellHtml(taskId);
}

/** 任务行所有单元格（按列 key 分发，两个视图共用的核心渲染逻辑）。
 *  projHtml：proj 列的单元格 HTML，由视图决定形态——board 首列分组 rowspan 合并、
 *  overdue 行内 compact；传 null 表示该行不渲染 proj 单元格。 */
function renderTaskCellsHtml(t, visibleCols, projHtml){
  let html = '';
  visibleCols.forEach((c) => {
    const attrs = cellModeAttrs(c);
    switch (c.key){
      case "proj":
        if (projHtml != null) html += projHtml;
        break;
      case "code":
        html += `<td class="cell-id"${attrs}><span class="id-tag">${escapeHtml(t.identifier || "-")}</span></td>`;
        break;
      case "title":
        html += `<td class="cell-title"${attrs}>${buildTaskTitle(t)}</td>`;
        break;
      case "desc": {
        // 描述：文本 + 图片缩略图行（点击 openLightbox 放大），hover title 显示全文
        const raw = (t.desc || "").trim();
        const imgs = Array.isArray(t.desc_images) ? t.desc_images : [];
        let descInner = raw ? `<span class="desc-text">${escapeHtml(raw)}</span>` : '';
        if (imgs.length) {
          descInner += '<span class="desc-imgs">'
            + imgs.map(u => `<img class="desc-img" src="${escapeHtml(u)}" loading="lazy" `
              + `alt="描述图片" title="点击放大查看" onclick="openLightbox(this.src)">`).join("")
            + '</span>';
        }
        html += `<td class="cell-desc"${attrs} title="${escapeHtml(raw)}">${descInner}</td>`;
        break;
      }
      case "status": {
        // 状态徽章：按展示名关键字判定色系（已完成 / 进行中 / 未开始 / 已取消）
        const s = (t.status || "").trim();
        let cls = "status-badge";
        if (s) {
          if (/完成|已结|交付|done|closed|关$/i.test(s))        cls += " is-done";
          else if (/进行|进行中|progress|doing/i.test(s))       cls += " is-progress";
          else if (/取消|cancel|报废/i.test(s))                  cls += " is-cancel";
          else                                                    cls += " is-pending";
        }
        html += `<td class="cell-status"${attrs}>`
          + (s ? `<span class="${cls}">${escapeHtml(s)}</span>` : '<span class="cell-empty">—</span>')
          + '</td>';
        break;
      }
      case "start": {
        const s = (t.start_at_str || "").trim();
        html += `<td class="cell-start"${attrs} title="${escapeHtml(s)}">`
          + (s && s !== "-" ? escapeHtml(fmtShortTime(s)) : '<span class="cell-empty">—</span>')
          + '</td>';
        break;
      }
      case "comments":
        html += `<td class="cell-comments" data-comments="${escapeHtml(t.task_id)}"${attrs}>${commentsCellHtml(t.task_id)}</td>`;
        break;
      case "owner": {
        // 未分配：空串或字面「未分配」都按 ghost 处理
        const raw = (t.assignee || "").trim();
        const unassigned = raw === "" || raw === "未分配";
        if (unassigned){
          html += `<td class="cell-owner"${attrs}>`
            + `<span class="owner unassigned" title="未分配"><span class="av"></span>`
            + `<span class="name">未分配</span></span></td>`;
        } else {
          const avCls = ownerColorClass(raw);
          const initial = raw.charAt(0).toUpperCase();
          html += `<td class="cell-owner"${attrs} title="${escapeHtml(raw)}">`
            + `<span class="owner"><span class="av ${avCls}">${escapeHtml(initial)}</span>`
            + `<span class="name">${escapeHtml(raw)}</span></span></td>`;
        }
        break;
      }
      case "updated":
        // 短格式 "08-20 19:45" 省年省秒；hover title 显示完整时间戳
        html += `<td class="cell-updated"${attrs} title="${escapeHtml(t.updated_at_str || t.updated_at || "—")}">`
          + `<span class="updated-time">${escapeHtml(fmtShortTime(t.updated_at_str) || "—")}</span></td>`;
        break;
      case "due":
        html += `<td class="cell-due"${attrs} title="${escapeHtml(t.due_at_str || "")}">`
          + `<span class="updated-time">${t.due_at_str && t.due_at_str !== "-"
              ? escapeHtml(fmtShortTime(t.due_at_str)) : "—"}</span></td>`;
        break;
      case "overdue": {
        // 逾期天数分级高亮：<3 天浅橙 / 3-7 天橙 / ≥7 天深红
        const d = t.overdue_days;
        if (d == null){
          html += `<td class="cell-overdue"${attrs}>—</td>`;
        } else {
          const lvl = d >= 7 ? " sev-high" : (d >= 3 ? " sev-mid" : " sev-low");
          html += `<td class="cell-overdue${lvl}"${attrs}>`
            + `<span class="overdue-num">${d}</span><span class="overdue-unit">天</span></td>`;
        }
        break;
      }
    }
  });
  return html;
}

/** 表格渲染完成后的公共收尾：列宽应用 → 搜索横幅 → 评论预取。
 *  列宽分配本身已交给 CSS（table-layout:auto + 每列 min-width），无需 JS 测量。 */
function afterTableRendered(items){
  // 把用户保存的自定义列宽同步到本次渲染出的所有 proj-table 上
  applyColumnWidths();

  // 搜索命中提示横幅（仅在有搜索关键字时显示）
  const kw = buildSearchKeyword();
  if (kw){
    const banner = document.createElement("div");
    banner.className = "search-banner";
    const parts = kw.split("|").filter(Boolean);
    const chipsHtml = parts.map(p => `「<b>${escapeHtml(p)}</b>」`).join(" OR ");
    banner.innerHTML = `正在按任务名称筛选 ${chipsHtml}`
      + (state.total != null ? `，命中 ${state.total} 条` : "") + `。`;
    const area = document.getElementById("tableArea");
    area.insertBefore(banner, area.firstChild);
  }

  // 渲染完成后，并发拉取所有未缓存任务的评论（带并发限流，避免瞬时压垮 API）
  const pending = items.map(t => t.task_id).filter(id => state.commentsCache[id] === undefined);
  if (pending.length) prefetchComments(pending);
}

function renderTable(items){
  // 负责人筛选：
  //   - board 视图：后端 /api/tasks 不支持 owner 过滤，必须前端 in-memory 过滤
  //   - overdue 视图：后端 /api/tasks/overdue 已经按 owner 过滤（避免双重过滤变 0 条，
  //     且让 badge total 与表格可见数完全一致，不会出现"数据对了数字不对"的脱节）
  if (state.view !== "overdue" && state.ownerFilter != null){
    items = items.filter(matchesOwnerFilter);
  }
  const area = document.getElementById("tableArea");
  if (!items.length){
    let msg;
    if (state.view === "overdue"){
      msg = state.ownerFilter != null
        ? "该负责人下暂无延期任务"
        : "该项目下暂无延期任务";
    } else {
      msg = state.ownerFilter != null
        ? "该负责人下暂无任务"
        : "该项目下暂无任务";
    }
    // 用 .empty（无 spinner）而不是 .loading（有旋转动画），
    // 避免空态看起来像一直在加载
    area.innerHTML = '<div class="empty">' + msg + '</div>';
    return;
  }
  // 两个视图统一走单表渲染（board 不再按项目拆多张卡片表）：
  // - 一张表只有一套 colgroup + thead，所有项目的同一列天然对齐
  // - 表头只出现一次（此前每个项目卡片各带一条 11 列表头，视觉堆叠）
  // - #tableArea 是滚动容器，thead 可吸顶（见 CSS）
  renderSingleTable(items, { projWidth: DEFAULT_COL_WIDTHS.proj });
}

/**
 * 单张统一表渲染（board / overdue 两个视图共用）。
 *
 * - proj 列每行单独显示（compact，色点 + 项目名 + 截断省略号）
 * - 项目切换处加 .project-change 分隔线，保留分组视觉
 * - 容器宽度自适应；总宽 > 容器时在 #tableArea 内横向滚动，列保持可读
 *
 * opts.projWidth：proj 列默认宽（board 用 200，overdue 用 150）。
 */
function renderSingleTable(items, opts){
  const visibleCols = computeVisibleCols();
  const projCol = visibleCols.find(c => c.key === "proj");
  const projAttrs = projCol ? cellModeAttrs(projCol) : '';
  const isOverdue = state.view === "overdue";
  const colgroupHtml = renderColgroupHtml(visibleCols, opts.projWidth);
  const theadHtml = renderTheadHtml(visibleCols);

  let html = `<div class="proj-block${isOverdue ? " overdue-block" : ""}">`;
  html += `<table class="proj-table${isOverdue ? " overdue-table" : ""}">${colgroupHtml}${theadHtml}<tbody>`;
  let prevProj = null;
  items.forEach((t) => {
    const projName = t.project_name || "(未命名项目)";
    const projChange = projName !== prevProj;
    prevProj = projName;
    const trCls = `task-row${projChange ? ' project-change' : ''}`;
    // 每行单独显示项目名（compact，色点 + 名称，截断省略号）
    const projHtml = projCol
      ? `<td class="proj-cell-inline"${projAttrs} title="${escapeHtml(projName)}">`
        + `<span class="proj-dot ${projColorClass(projName)}"></span>`
        + `<span class="proj-name">${escapeHtml(projName)}</span></td>`
      : null;
    html += `<tr class="${trCls}" data-id="${escapeHtml(t.task_id)}">`
      + renderTaskCellsHtml(t, visibleCols, projHtml) + '</tr>';
  });
  html += '</tbody></table></div>';

  document.getElementById("tableArea").innerHTML = html;
  afterTableRendered(items);
}

function toggleComments(taskId){
  // 详情行已去除，保留空函数避免外部历史代码报错
}

async function prefetchComments(taskIds){
  // 并发限流：每次最多 6 个并发请求
  const CONCURRENCY = 6;
  let i = 0;
  const workers = Array.from({length: Math.min(CONCURRENCY, taskIds.length)}, async () => {
    while (i < taskIds.length){
      const id = taskIds[i++];
      await loadComments(id);
    }
  });
  await Promise.all(workers);
}

async function loadComments(taskId){
  const cell = document.querySelector(`td.cell-comments[data-comments="${cssEscape(taskId)}"]`);
  if (!cell) return;
  try {
    const data = await api("/api/tasks/" + encodeURIComponent(taskId) + "/comments");
    state.commentsCache[taskId] = data.comments || [];
    cell.innerHTML = commentsCellHtml(taskId);
  } catch(e){
    cell.innerHTML = `<div class="cell-error">${escapeHtml(e.message)}</div>`;
  }
}

/** 渲染单条评论块（附件 / emoji-only 判定 / 富文本内容）。 */
function commentHtml(c){
  {
    const atts = (c.attachments || []).map(a => {
      const name = escapeHtml(a.name || "未命名");
      if (a.file_id) {
        const fid = encodeURIComponent(a.file_id);
        if (a.is_image) {
          // 图片：缩略图 + 点击放大
          return `<span class="att img-type">`
            + `<img class="att-thumb" src="/api/file/${fid}" loading="lazy" alt="${name}" `
            + `title="点击放大查看" onclick="openLightbox(this.src)">`
            + `<span class="fname">${name}</span></span>`;
        }
        // 文件：新标签打开预览 / 下载
        return `<a class="att file-type" href="/api/file/${fid}" target="_blank" `
          + `rel="noopener" title="点击预览 / 下载">📄 <span class="fname">${name}</span></a>`;
      }
      // 无 file_id（极少数情况）：降级为纯文本标签
      const cls = a.is_image ? "att img-type" : "att file-type";
      const label = a.is_image ? "图片" : "文件";
      return `<span class="${cls}"><span>${label}</span>`
        + `<span class="fname">${name}</span></span>`;
    }).join("");
    // eslint-disable-next-line no-control-regex
  const EMOJI_ONLY = /^[\s\u200d\ufe0f]*([\p{Extended_Pictographic}\u200d\ufe0f]+[\s\u200d\ufe0f]*){1,6}$/u;
  const isEmojiOnly = (s) => typeof s === "string" && EMOJI_ONLY.test(s.trim());
  const bodyHtml = renderContent(c.content || "");
  const bodyCls = isEmojiOnly(c.content || "") ? "body emoji-only" : "body";
    return `<div class="comment-block">`
      + `<div class="meta"><b>${escapeHtml(c.author)}</b>·${escapeHtml(c.created_at_str)}</div>`
      + `<div class="${bodyCls}">${bodyHtml}</div>`
      + (atts ? `<div class="atts">${atts}</div>` : "")
      + '</div>';
  }
}

/**
 * 把评论纯文本渲染成结构化 HTML：
 *  - ```lang\n...\n``` markdown 代码块 → <pre class="code-block">
 *  - 行内 `code` → <code>code</code>
 *  - 其他段落合并换行 → <p>...</p>
 */
function renderContent(text){
  if (!text) return "";
  // 1) 先抽出 ``` 代码块（避免后续行内 code / escape 影响它），用占位符替入
  const blocks = [];
  let s = escapeHtml(text).replace(
    /```([a-zA-Z0-9+_\-]*)[ \t]*\n([\s\S]*?)```/g,
    (_, lang, code) => {
      const idx = blocks.length;
      blocks.push({ lang: lang || "", code });
      return "\u0000CB" + idx + "\u0000";
    }
  );
  // 1.5) 把 URL 渲染成可点击链接（这一步放在代码块占位之后，避免 code 块里的 URL 被错误链接化）。
  //      URL 终止字符：空白 / 引号 / 括号 / 中括号 / 大括号 / 中文逗号 / < 等。
  //      escapeHtml 已经把 < > " ' 转义，所以正则无需再考虑这些字符本身；为保险仍写入字符集。
  s = s.replace(
    /\bhttps?:\/\/[^\s<>"'`)\]}\,、，；。!！]+/g,
    (url) => `<a class="cmt-link" href="${url}" target="_blank" rel="noopener noreferrer" title="新标签打开：${url}">${url}</a>`
  );
  // 2) 行内 `code`
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  // 3) 按行处理：连续非空行合并成 <p>，占位符独立成 <pre>
  const lines = s.split("\n");
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) {
      out.push("<p>" + para.join("<br>") + "</p>");
      para = [];
    }
  };
  for (const line of lines){
    const m = line.match(/^\u0000CB(\d+)\u0000$/);
    if (m){
      flush();
      const b = blocks[+m[1]];
      const lang = b.lang ? `<span class="lang">${b.lang}</span>` : "";
      out.push(`<pre class="code-block">${lang}${b.code}</pre>`);
      continue;
    }
    para.push(line);
  }
  flush();
  return out.join("");
}

function renderPager(){
  const pager = document.getElementById("pager");
  const curr = state.page + 1;
  let totalHtml = "";
  let maxPage = null;

  if (state.total != null){
    maxPage = Math.max(1, Math.ceil(state.total / state.pageSize));
    totalHtml = `<span class="info">共 ${state.total} 条 · 共 ${maxPage} 页</span>`;
  } else if (state.hasMore){
    totalHtml = `<span class="info">第 ${curr} 页（还有更多）</span>`;
  } else {
    totalHtml = `<span class="info">第 ${curr} 页</span>`;
  }

  // 跳转输入框：total 已知时设 max；hasMore 但 total 未知时也放开可输入（不写 max）；
  // 两边都为否（首屏单页）才置灰。
  const enableJump = (state.total != null) || state.hasMore;
  const jumpMaxAttr = (state.total != null) ? `max="${maxPage}"` : "";
  const jumpHint = (!state.hasMore && state.total == null) ? `placeholder="-"`
                  : (state.total == null && state.hasMore) ? `placeholder="N"`
                  : "";

  pager.innerHTML = `
    <button class="pager-btn" id="prevBtn" ${state.page<=0?'disabled':''} onclick="gotoPage(-1)">上一页</button>
    ${totalHtml}
    <span class="page-jump">
      <span>前往</span>
      <input id="pageInput" class="page-input" type="number" min="1"
             ${jumpMaxAttr} value="${curr}" ${jumpHint}
             ${enableJump ? "" : "disabled"}
             onkeydown="if(event.key==='Enter'){gotoExactPage();}">
      <span>页</span>
      <button class="page-go" onclick="gotoExactPage()" ${enableJump ? "" : "disabled"}>跳转</button>
    </span>
    <button class="pager-btn" id="nextBtn" ${state.hasMore?'':'disabled'} onclick="gotoPage(1)">下一页</button>
  `;
}

function gotoExactPage(){
  if (state.total == null && !state.hasMore) return;
  const input = document.getElementById("pageInput");
  if (!input) return;
  let n = parseInt(input.value, 10);
  if (!Number.isFinite(n) || n < 1) { input.value = state.page + 1; return; }
  if (state.total != null){
    const maxPage = Math.max(1, Math.ceil(state.total / state.pageSize));
    if (n > maxPage) n = maxPage;
  }
  input.value = n;
  const target = n - 1;
  if (target === state.page) return;
  state.page = target;
  loadTasks();
}

function gotoPage(delta){
  const next = state.page + delta;
  if (next < 0) return;
  if (delta > 0 && !state.hasMore) return;
  state.page = next;
  loadTasks();
}

function cssEscape(s){
  return String(s).replace(/["\\]/g, "\\$&");
}

/** 完整时间 "2026-08-20 19:45:39" → 短格式 "08-20 19:45"（hover title 仍显示完整时间）。
 *  非该形态（"-" / 空等）原样返回。 */
function fmtShortTime(fullStr){
  const s = String(fullStr || "");
  const m = s.match(/^(\d{4})-(\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return m ? `${m[2]} ${m[3]}` : s;
}

// 回车登录
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && !document.getElementById("loginView").classList.contains("hidden")){
    doLogin();
  }
});

// 页面加载：若已有有效会话则自动进入看板，否则显示登录页（解决每次都要输入凭证）
async function bootstrap(){
  loadColumnWidths();    // 恢复用户上次拖拽调整过的列宽
  loadColumnConfig();    // 恢复用户上次自定义的列显隐与顺序
  // 快照主看板布局（进入延期视图时切换，切回时恢复）
  state.boardColOrder = state.colOrder.slice();
  state.boardColVisible = { ...state.colVisible };
  state.boardColModes = { ...state.colModes };
  bindColumnResize();    // 全局一次性绑定列宽拖拽监听
  try {
    const me = await api("/api/me");      // 401 会自动跳回登录页并抛错
    if (me.tenant_host) state.tenantHost = me.tenant_host;
    if (me.client_id_masked) state.clientIdMasked = me.client_id_masked;
    if (me.tenant_fingerprint) state.tenantFingerprint = me.tenant_fingerprint;
    renderTenantBadge();
    hide("loginView"); show("boardView");
    await loadProjects();
  } catch(e){
    hide("boardView"); show("loginView");
  }
}
window.addEventListener("DOMContentLoaded", bootstrap);

// 图片放大预览（lightbox）
function openLightbox(src){
  document.getElementById("lightboxImg").src = src;
  document.getElementById("lightbox").classList.add("active");
}
function closeLightbox(){
  const lb = document.getElementById("lightbox");
  lb.classList.remove("active");
  document.getElementById("lightboxImg").src = "";
}
// ESC 关闭放大层
document.addEventListener("keydown", e => { if (e.key === "Escape") closeLightbox(); });

// 初始化搜索式项目选择器
initProjectPicker();
// 初始化搜索式负责人选择器
initOwnerPicker();

/* ====================== 任务名称 typeahead + 多选 chip ====================== */

/** 把当前 chips 与草稿合并成 OR 的 keyword 字符串（用于 URL 参数）。 */
function buildSearchKeyword(){
  const parts = state.titleChips.map(c => c.title).filter(Boolean);
  const draft = (state.titleDraft || "").trim();
  if (draft) parts.push(draft);
  // 去重（保持原顺序）
  const seen = new Set(); const out = [];
  for (const p of parts){ if (!seen.has(p)) { seen.add(p); out.push(p); } }
  return out.join("|");
}

/** 触发一次任务匹配（点查询按钮 / 选中候选后回车 / 草稿回车）。 */
async function onChipSearch(){
  closeTitleMenu();
  state.titleDraft = "";
  const input = document.getElementById("titleSearch");
  if (input) input.value = "";
  updateChipSearchState();
  state.page = 0; state.expanded.clear(); state.commentsCache = {}; state.commentsExpanded = new Set();
  await loadTasks();
}

/** 清除所有 chips + 草稿，重新加载全量。 */
async function clearTitleSearch(){
  state.titleChips = [];
  state.titleDraft = "";
  state.titleCandidates = [];
  state.titleIdx = -1;
  // 清空 input DOM（包括屏幕上看不到、但 value 残留的字符）
  const input = document.getElementById("titleSearch");
  if (input){
    input.value = "";
    // 主动派发 input 事件，让 onTitleInputChange 等任何监听者重新计算 has-value 等样式
    input.dispatchEvent(new Event("input", {bubbles: true}));
  }
  renderChips();           // 把 DOM 里的 chip 元素也实际移除（之前漏了，state 清了 DOM 还在）
  closeTitleMenu();
  updateChipSearchState();
  state.page = 0; state.expanded.clear(); state.commentsCache = {}; state.commentsExpanded = new Set();
  await loadTasks();
}

/** 移除指定 chip（按 id），同时清掉输入框里的草稿并刷新表格。 */
function removeChip(taskId){
  state.titleChips = state.titleChips.filter(c => c.task_id !== taskId);
  state.titleDraft = "";
  const input = document.getElementById("titleSearch");
  if (input){
    input.value = "";
    input.dispatchEvent(new Event("input", {bubbles: true}));
  }
  renderChips();
  updateChipSearchState();
  // 移除一个 chip 后，剩余 chips 仍作为筛选条件，让表格立刻反映新过滤集
  state.page = 0; state.expanded.clear(); state.commentsCache = {}; state.commentsExpanded = new Set();
  loadTasks();
}

/** 渲染 chips 区。 */
function renderChips(){
  const area = document.getElementById("chipArea");
  area.innerHTML = "";
  state.titleChips.forEach(chip => {
    const el = document.createElement("span");
    el.className = "chip";
    el.title = (chip.project_name ? `【${chip.project_name}】 ` : "") + chip.title
              + (chip.identifier ? `（${chip.identifier}）` : "");
    const meta = chip.project_name
      ? `<span class="chip-meta">${escapeHtml(chip.project_name)}</span>` : "";
    el.innerHTML = meta + escapeHtml(chip.title)
      + `<button class="chip-close" title="移除" data-id="${escapeHtml(chip.task_id)}">×</button>`;
    el.querySelector(".chip-close").addEventListener("click", (e) => {
      e.stopPropagation();
      removeChip(chip.task_id);
    });
    area.appendChild(el);
  });
}

/** 根据当前 chips/draft 更新清除按钮与 query 按钮的启用状态。 */
function updateChipSearchState(){
  const wrap = document.getElementById("chipSearch");
  const has = state.titleChips.length > 0 || (state.titleDraft || "").trim().length > 0;
  wrap.classList.toggle("has-value", has);
}

/** 输入框变化：保存草稿、防抖触发候选请求。 */
function onTitleInputChange(){
  const input = document.getElementById("titleSearch");
  state.titleDraft = input.value;
  updateChipSearchState();
  clearTimeout(state.titleDebounceTimer);
  const q = state.titleDraft.trim();
  if (!q){
    closeTitleMenu();
    return;
  }
  state.titleDebounceTimer = setTimeout(fetchTitleCandidates, state.titleDebounceMs);
}

async function fetchTitleCandidates(){
  const q = state.titleDraft.trim();
  if (!q || !state.projectId){ closeTitleMenu(); return; }
  try {
    const data = await api(`/api/task_titles?project_id=${encodeURIComponent(state.projectId)}`
      + `&q=${encodeURIComponent(q)}&limit=15`);
    state.titleCandidates = data.items || [];
    state.titleIdx = state.titleCandidates.length ? 0 : -1;
    openTitleMenu();   // 先显示 + 定位；空结果也会显示「没有匹配」提示，避免「什么都不弹」
    renderTitleMenu();
  } catch(e){
    console.error("fetchTitleCandidates failed:", e);
    closeTitleMenu();
  }
}

function renderTitleMenu(){
  const menu = document.getElementById("titleMenu");
  menu.innerHTML = "";
  // 浮层标题（始终显示，让用户知道这是「候选」不是「全局查询结果」）
  const title = document.createElement("div");
  title.className = "menu-title";
  const n = state.titleCandidates.length;
  title.innerHTML = `<span class="dot"></span>任务候选 · ${n ? n + " 项匹配" : "实时匹配"}
    <span style="flex:1"></span>
    <span style="font-size:11px;color:#94a3b8">输入即搜</span>`;
  menu.appendChild(title);
  if (!n){
    const empty = document.createElement("div");
    empty.className = "opt-empty";
    empty.textContent = "没有匹配的任务；直接按 Enter 会把当前草稿当关键字执行全局查询";
    menu.appendChild(empty);
  } else {
    state.titleCandidates.forEach((cand, i) => {
      const div = document.createElement("div");
      div.className = "opt" + (i === state.titleIdx ? " highlighted" : "");
      div.dataset.idx = String(i);
      const titleHtml = highlightInline(cand.title || "", state.titleDraft);
      const meta = (cand.project_name ? `【${cand.project_name}】` : "")
        + (cand.identifier ? ` · ${cand.identifier}` : "");
      div.innerHTML =
        `<div>${titleHtml}</div>`
        + (meta ? `<div class="opt-meta">${escapeHtml(meta)}</div>` : "");
      div.addEventListener("mouseenter", () => {
        state.titleIdx = i; updateTitleHighlight();
      });
      div.addEventListener("mousedown", (e) => {
        // 用 mousedown 避免 input blur 先触发、菜单先关闭
        e.preventDefault();
        pickTitleCandidate(i);
      });
      menu.appendChild(div);
    });
  }
  // 底部使用提示
  const hint = document.createElement("div");
  hint.className = "menu-hint";
  hint.textContent = "↑↓ 选择 · Enter 加入查询 · Esc 关闭";
  menu.appendChild(hint);
}

function updateTitleHighlight(){
  const items = document.querySelectorAll("#titleMenu .opt");
  items.forEach((el, i) => el.classList.toggle("highlighted", i === state.titleIdx));
}

function openTitleMenu(){
  const menu = document.getElementById("titleMenu");
  positionMenuUnder(menu, document.getElementById("titleSearch"));
  menu.hidden = false;
}
function closeTitleMenu(){
  const menu = document.getElementById("titleMenu");
  if (menu){ menu.hidden = true; menu.innerHTML = ""; }
  state.titleCandidates = [];
  state.titleIdx = -1;
}

/** 在输入框下方/候选列表里给命中关键字加高亮（先转义再包标签）。 */
function highlightInline(text, kw){
  const safe = escapeHtml(text);
  const q = (kw || "").trim();
  if (!q) return safe;
  const lowerText = text.toLowerCase();
  const lowerQ = q.toLowerCase();
  let out = "";
  let i = 0, idx;
  while ((idx = lowerText.indexOf(lowerQ, i)) !== -1){
    // 由于 safe 是转义后的，索引仍然按原字符算（escapeHtml 不增删字符）
    out += safe.slice(i, idx);
    out += '<span class="hl">' + safe.slice(idx, idx + q.length) + '</span>';
    i = idx + q.length;
  }
  out += safe.slice(i);
  return out;
}

/** 从候选中选一项加入 chip（重复任务不重复加）。 */
async function pickTitleCandidate(idx){
  const cand = state.titleCandidates[idx];
  if (!cand) return;
  if (!state.titleChips.some(c => c.task_id === cand.task_id)){
    state.titleChips.push({
      task_id: cand.task_id,
      identifier: cand.identifier,
      title: cand.title,
      project_name: cand.project_name,
    });
  }
  // 选中后清掉输入框和草稿，让用户继续添加
  state.titleDraft = "";
  const input = document.getElementById("titleSearch");
  if (input) input.value = "";
  closeTitleMenu();
  renderChips();
  updateChipSearchState();
  // 让输入框聚焦，保持流畅添加
  if (input) input.focus();
}

/** 输入框键盘事件：↑↓ Enter Esc。 */
function onTitleKey(e){
  const menu = document.getElementById("titleMenu");
  if (e.key === "ArrowDown"){
    if (menu.hidden && state.titleDraft.trim()) fetchTitleCandidates();
    if (!state.titleCandidates.length) return;
    state.titleIdx = Math.min(state.titleIdx + 1, state.titleCandidates.length - 1);
    updateTitleHighlight();
    const el = menu.querySelector(".opt.highlighted");
    if (el) el.scrollIntoView({block:"nearest"});
    e.preventDefault();
  } else if (e.key === "ArrowUp"){
    if (!state.titleCandidates.length) return;
    state.titleIdx = Math.max(state.titleIdx - 1, 0);
    updateTitleHighlight();
    const el = menu.querySelector(".opt.highlighted");
    if (el) el.scrollIntoView({block:"nearest"});
    e.preventDefault();
  } else if (e.key === "Enter"){
    e.preventDefault();
    if (!menu.hidden && state.titleIdx >= 0 && state.titleCandidates[state.titleIdx]){
      pickTitleCandidate(state.titleIdx);
    } else {
      // 无选中候选：把当前草稿当 keyword 触发查询
      onChipSearch();
    }
  } else if (e.key === "Escape"){
    if (!menu.hidden){ closeTitleMenu(); e.preventDefault(); }
  }
}

function initTitleSearch(){
  const input = document.getElementById("titleSearch");
  const wrap = document.getElementById("chipSearch");
  input.addEventListener("input", onTitleInputChange);
  input.addEventListener("keydown", onTitleKey);
  input.addEventListener("focus", () => {
    // 只要草稿非空就拉一次候选，确保用户重新聚焦也能看到结果
    if (state.titleDraft.trim()) fetchTitleCandidates();
  });
  // 点击外部关闭菜单
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) closeTitleMenu();
  });
  // fixed 浮层需要跟随滚动/缩放/窗口变化重新对齐（否则会跑位）
  const reposition = () => {
    const menu = document.getElementById("titleMenu");
    if (menu && !menu.hidden) positionMenuUnder(menu, input);
  };
  window.addEventListener("scroll", reposition, true);
  window.addEventListener("resize", reposition);
  renderChips();
  updateChipSearchState();
}

initTitleSearch();

/* ====================== 列设置弹窗 ====================== */

/** 打开弹窗：渲染列设置列表。 */
function openColumnSettings(){
  const ul = document.getElementById("colSettingsList");
  ul.innerHTML = "";
  // 按当前 colOrder 顺序渲染（proj 永远在最上面作为锚定）
  state.colOrder.forEach((key, idx) => {
    ul.appendChild(buildColumnSettingRow(key, idx));
  });
  document.getElementById("colModalMask").classList.remove("hidden");
  bindColumnRowDrag();
}
function closeColumnSettings(){
  document.getElementById("colModalMask").classList.add("hidden");
}
/** 点遮罩层关闭（但点 modal 卡片内部不会冒泡到 mask）。 */
function onColModalMaskClick(e){
  if (e.target.id === "colModalMask") closeColumnSettings();
}

/** 重置列设置：清空所有用户改动，回全部列显 + 默认顺序 + 默认宽度 + 默认模式。 */
function resetColumnSettings(){
  state.colOrder = [...DEFAULT_COL_ORDER];
  for (const c of COLS_META) state.colVisible[c.key] = true;
  state.colModes = { ...DEFAULT_COL_MODES };
  state.colModes["proj"] = "fixed";   // 锚定列永远固定
  state.colWidths = {};
  saveColumnConfig();
  saveColumnWidths();
  // 重新渲染弹窗列表（让 UI 与默认状态一致）
  openColumnSettings();
  // 重新渲染主表格（应用默认列宽 + 默认列集合 + 默认模式）
  applyColumnConfig();
}

/** 创建弹窗里的一行（li.col-row）。 */
function buildColumnSettingRow(key, idx){
  const meta = COLS_META.find(m => m.key === key);
  const isAnchor = !!meta.anchor || !!meta.endAnchor;
  const li = document.createElement("li");
  li.className = "col-row" + (isAnchor ? " anchor" : "");
  li.dataset.key = key;
  li.dataset.originalIdx = String(idx);
  if (!state.colVisible[key]) li.classList.add("is-hidden");

  // 拖拽手柄（anchored 行 disable 视觉）
  const handle = document.createElement("span");
  handle.className = "drag-handle";
  handle.textContent = "⋮⋮";
  if (isAnchor) handle.setAttribute("aria-disabled", "true");

  // 复选框（anchor 行 disabled）
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = state.colVisible[key];
  cb.disabled = isAnchor;
  cb.addEventListener("change", () => {
    state.colVisible[key] = cb.checked;
    li.classList.toggle("is-hidden", !cb.checked);
    applyColumnConfig();
  });

  // 列名
  const label = document.createElement("span");
  label.className = "col-label";
  label.textContent = meta.label;

  // 端点标签（proj=左端点 / updated=右端点）
  const tag = isAnchor ? document.createElement("span") : null;
  if (tag){
    tag.className = "col-tag";
    if (meta.anchor){
      tag.textContent = "左端点";
      tag.title = "项目名称列用于分组合并，固定为第 1 列且不可隐藏";
    } else {  // endAnchor
      tag.textContent = "右端点";
      tag.title = "最近更新时间列固定为最后 1 列且不可隐藏";
    }
  }

  // 模式切换器：固定（可拖拽调宽）/ 自适应（按内容计算）
  const modeSwitch = document.createElement("div");
  modeSwitch.className = "mode-switch";
  const curMode = state.colModes[key] || meta.mode || "auto";
  // 端点列 + 中间 auto 列（code/title/owner）禁用切换 —— 这些列永远是 auto，
  // 防止用户把它们误切成 fixed 后又出现"拖拽遮挡文字"的问题。
  const switchDisabled = isAnchor || ["code", "title", "owner"].includes(key);
  for (const mode of ["fixed", "auto"]){
    const opt = document.createElement("label");
    opt.className = "mode-opt" + (mode === curMode ? " active" : "");
    opt.dataset.mode = mode;
    opt.title = mode === "fixed"
      ? "固定列宽：可拖拽表头右侧手柄手动调节"
      : "自适应列宽：按当前内容自动算列宽";
    if (switchDisabled) opt.classList.add("disabled");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = `colmode-${key}`;
    radio.value = mode;
    radio.checked = (mode === curMode);
    radio.disabled = switchDisabled;
    opt.appendChild(radio);
    const text = document.createElement("span");
    text.textContent = mode === "fixed" ? "固定" : "自适应";
    opt.appendChild(text);
    opt.addEventListener("click", e => {
      if (switchDisabled){ e.preventDefault(); return; }
      if (state.colModes[key] === mode) return;
      state.colModes[key] = mode;
      // 更新 group 内的 active 状态
      modeSwitch.querySelectorAll(".mode-opt").forEach(o => {
        o.classList.toggle("active", o.dataset.mode === mode);
      });
      applyColumnConfig();
    });
    modeSwitch.appendChild(opt);
  }

  li.appendChild(handle);
  li.appendChild(cb);
  li.appendChild(label);
  if (tag) li.appendChild(tag);
  li.appendChild(modeSwitch);
  return li;
}

/* -------- 弹窗内拖拽排序实现 -------- */
let _colRowDrag = null;   // { srcLi, startY, srcRect }
function bindColumnRowDrag(){
  const list = document.getElementById("colSettingsList");
  // 解绑老监听（避免重复绑定）：直接 cloneNode 替换 element
  // 但保留 children 状态复杂，更稳妥做法是用一组 flag 标记，仅首次绑定。
  if (list.dataset.dragBound === "1") return;
  list.dataset.dragBound = "1";

  // 计算当前 hover 位置（目标 li 上半 / 下半）
  const computeHoverTarget = (clientY) => {
    const items = [...list.querySelectorAll(".col-row:not(.dragging)")];
    for (const el of items){
      const r = el.getBoundingClientRect();
      if (clientY < r.top + r.height / 2){
        return { el, side: "top" };
      }
    }
    if (items.length){
      return { el: items[items.length - 1], side: "bot" };
    }
    return { el: null, side: null };
  };

  list.addEventListener("mousedown", e => {
    const handle = e.target.closest(".drag-handle");
    if (!handle) return;
    const li = handle.closest(".col-row");
    if (!li || li.classList.contains("anchor")) return;   // 锚定行不可拖
    if (e.button !== 0) return;
    e.preventDefault();
    _colRowDrag = {
      srcLi: li,
      startY: e.clientY,
      srcRect: li.getBoundingClientRect(),
    };
    li.classList.add("dragging");
    document.body.classList.add("col-resizing");   // 复用全局禁选样式（避免选词）
  });

  document.addEventListener("mousemove", e => {
    if (!_colRowDrag) return;
    // 清除所有高亮
    list.querySelectorAll(".col-row").forEach(r => {
      r.classList.remove("drag-over-top", "drag-over-bot");
    });
    const { el, side } = computeHoverTarget(e.clientY);
    if (el && el !== _colRowDrag.srcLi){
      // 禁止越过 anchor 行：anchor 行的上方永远不能插入
      if (el.classList.contains("anchor")){
        if (side === "top") return;   // 试图放到 anchor 上方 → 不响应
      }
      el.classList.add(side === "top" ? "drag-over-top" : "drag-over-bot");
    }
  });

  document.addEventListener("mouseup", e => {
    if (!_colRowDrag) return;
    const { srcLi } = _colRowDrag;
    const { el, side } = computeHoverTarget(e.clientY);
    // 清理高亮
    list.querySelectorAll(".col-row").forEach(r => {
      r.classList.remove("drag-over-top", "drag-over-bot");
    });
    srcLi.classList.remove("dragging");
    document.body.classList.remove("col-resizing");

    // 真正的 DOM 重排 + state 更新
    if (el && el !== srcLi){
      // 若目标在 anchor 行上方，禁止
      if (el.classList.contains("anchor") && side === "top"){
        _colRowDrag = null;
        return;
      }
      if (side === "top") el.parentNode.insertBefore(srcLi, el);
      else el.parentNode.insertBefore(srcLi, el.nextSibling);
      // 同步 state.colOrder
      const newOrder = [...list.querySelectorAll(".col-row")].map(r => r.dataset.key);
      state.colOrder = newOrder;
      applyColumnConfig();
    }
    _colRowDrag = null;
  });
}
