const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
const text = new Intl.NumberFormat("zh-CN");
const actions = { draw: "首次抽取", redraw: "更换角色", marry: "结婚", divorce: "离婚" };
const scopes = { session: "会话", group: "群聊或私聊", user: "跨群共享" };
const platforms = { qq_official: "QQ 官 Bot", aiocqhttp: "OneBot" };
const views = {
  history: { title: "抽取与关系流水", columns: ["时间", "会话", "玩家", "操作", "角色"] },
  players: { title: "玩家与婚姻", columns: ["玩家", "最近会话", "当前角色", "婚姻", "抽取统计", "管理"] },
};
const model = {
  view: "history", page: 1, q: "", action: "", player: null, timezone: "Asia/Shanghai",
  group: null, groupPage: 1, groupQuery: "",
};
let listRequest = 0;
let overviewRequest = 0;
let groupsRequest = 0;
let storeBusy = false;
let resetTarget = null;
let aliasBusy = false;
let aliasTarget = null;

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value != null) node.textContent = String(value);
  if (className) node.className = className;
  return node;
}

function button(label, onClick) {
  const node = element("button", label, "secondary");
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function describe(label, detail) {
  const container = element("span");
  container.append(element("strong", label));
  if (detail) container.append(element("span", detail, "detail"));
  return container;
}

function character(value) {
  return value ? describe(value.name || "未知角色", value.source) : element("span", "—");
}

function playerName(player) {
  return player.display_name || player.user_id || "旧版玩家";
}

function playerCell(player, sessionId = player.session_id) {
  const identity = [platformName(player), scopes[player.scope]].filter(Boolean).join(" / ");
  const info = [player.display_name && player.user_id, identity, sessionId].filter(Boolean).join(" · ");
  const fallback = player.key ? `旧版标识末尾 ${player.key.slice(-8)}` : "旧版未记录身份";
  return describe(playerName(player), info || fallback);
}

function platformName(entry) {
  return [platforms[entry.platform] || entry.platform, entry.platform_id].filter(Boolean).join(" / ");
}

function groupName(group) {
  if (!group) return "全部记录";
  if (group.group_id) {
    if (group.group_name) return group.group_name;
    const id = group.group_id;
    return id.length <= 16 ? `群 ${id}` : `群 ${id.slice(0, 8)}…${id.slice(-4)}`;
  }
  return group.conversation_type === "private" ? "私聊" : "未归类记录";
}

function groupDetail(group) {
  if (!group?.group_id) return "";
  const showFullId = group.group_name || group.group_id.length > 16;
  return [showFullId && `群号 ${group.group_id}`, platformName(group)].filter(Boolean).join(" · ");
}

function groupCell(group) {
  const name = groupName(group);
  const detail = groupDetail(group);
  if (!group.group_key || group.group_key === model.group?.group_key) return describe(name, detail);
  const container = element("div");
  const control = element("button", name, "group-link");
  control.type = "button";
  control.addEventListener("click", () => {
    selectGroup(group);
    $("active-group").focus({ preventScroll: true });
  });
  container.append(control);
  if (detail) container.append(element("span", detail, "detail"));
  return container;
}

function dateTime(seconds, fallback = "—") {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return fallback;
  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hourCycle: "h23", timeZone: model.timezone,
  }).format(date);
}

function columnLabels(view) {
  return views[view].columns.map((title) => model.group && title === "最近会话" ? "所选会话" : title);
}

function row(view, nodes) {
  const result = element("tr");
  const labels = columnLabels(view);
  nodes.forEach((node, index) => {
    const cell = element("td");
    cell.dataset.label = labels[index];
    cell.append(node);
    result.append(cell);
  });
  return result;
}

function historyRow(record) {
  return row("history", [
    element("time", dateTime(record.at, record.date || "—")),
    groupCell(record),
    playerCell(record),
    element("span", actions[record.action] || record.action || "未知操作", "badge"),
    character(record.character),
  ]);
}

function playerRow(player) {
  const controls = element("div", null, "row-actions");
  controls.append(
    button("查看流水", () => selectView("history", { key: player.key, name: playerName(player) })),
    button("重置次数", () => openReset(player)),
  );
  const marriage = player.marriage;
  const sessionId = model.group && player.scope === "user" ? "" : player.session_id;
  return row("players", [
    playerCell(player, sessionId),
    groupCell(model.group || player),
    character(marriage?.wife || player.wife),
    marriage ? describe("有效婚姻", `到期：${dateTime(marriage.expires_at)}`) : element("span", "未婚", "badge neutral"),
    describe(`累计 ${text.format(player.draws ?? 0)} 次`, `今日已更换 ${text.format(player.redraw_count ?? 0)} 次`),
    controls,
  ]);
}

function feedback(message = "", error = false) {
  $("feedback").textContent = message;
  $("feedback").classList.toggle("error", error);
}

function updateTabs() {
  for (const name of Object.keys(views)) $(name + "-tab").setAttribute("aria-pressed", name === model.view);
  $("action-filter").hidden = model.view !== "history";
  $("query").placeholder = model.view === "history" ? "玩家、角色或出处" : "昵称、玩家 ID 或会话";
  $("focused-player").hidden = !model.player;
  $("focused-player").textContent = model.player ? `正在查看：${model.player.name} 的流水` : "";
  $("record-caption").textContent = views[model.view].title;
  const head = element("tr");
  for (const title of columnLabels(model.view)) {
    const header = element("th", title);
    header.scope = "col";
    head.append(header);
  }
  $("record-head").replaceChildren(head);
}

function renderListing(listing, view) {
  $("record-body").replaceChildren(...listing.items.map(view === "history" ? historyRow : playerRow));
  $("empty").hidden = listing.items.length > 0;
  $("empty").textContent = model.q || model.action || model.player || model.group
    ? "没有符合筛选条件的记录。"
    : "暂无记录。玩家抽取角色后，数据会出现在这里。";
  const totalPages = Math.max(1, Math.ceil(listing.total / listing.size));
  $("page-label").textContent = `共 ${text.format(listing.total)} 条 · 第 ${listing.page} / ${totalPages} 页`;
  $("previous").disabled = listing.page <= 1;
  $("next").disabled = listing.page >= totalPages;
}

async function loadListing() {
  const current = ++listRequest;
  const view = model.view;
  const parameters = { page: model.page, q: model.q };
  if (model.group) parameters.group_key = model.group.group_key;
  if (view === "history") {
    parameters.action = model.action;
    if (model.player) parameters.player_key = model.player.key;
  }
  $("records").setAttribute("aria-busy", "true");
  $("record-body").replaceChildren();
  $("empty").hidden = false;
  $("empty").textContent = "正在读取…";
  $("page-label").textContent = "—";
  $("previous").disabled = $("next").disabled = true;
  try {
    const listing = await bridge.apiGet(`ledger/${view}`, parameters);
    if (current !== listRequest) return;
    const lastPage = Math.max(1, Math.ceil(listing.total / listing.size));
    if (listing.page > lastPage) {
      model.page = lastPage;
      await loadListing();
      return;
    }
    renderListing(listing, view);
  } catch (error) {
    if (current !== listRequest) return;
    $("empty").textContent = "读取失败，请刷新重试。";
    feedback(error?.message || "读取账本失败。", true);
  } finally {
    if (current === listRequest) $("records").setAttribute("aria-busy", "false");
  }
}

function updateGroupSelection() {
  const detail = groupDetail(model.group);
  $("active-group").textContent = groupName(model.group);
  $("active-group-detail").textContent = detail;
  $("active-group-detail").hidden = !detail;
  $("edit-group").hidden = !model.group?.group_id;
  $("all-groups").setAttribute("aria-pressed", !model.group);
  for (const option of $("group-list").children) {
    option.setAttribute("aria-pressed", option.dataset.key === model.group?.group_key);
  }
}

function selectGroup(group) {
  if (group?.group_key === model.group?.group_key) return;
  model.group = group;
  model.player = null;
  model.page = 1;
  feedback();
  updateGroupSelection();
  updateTabs();
  $("active-group").scrollIntoView({ block: "nearest" });
  void loadListing();
}

function renderGroups(listing) {
  $("group-list").replaceChildren(...listing.items.map((group) => {
    const option = element("button", null, "group-option");
    option.type = "button";
    option.dataset.key = group.group_key;
    const count = element("span", `${text.format(group.history_count)} 条`, "group-count");
    count.title = `${text.format(group.player_count)} 位玩家 · ${text.format(group.history_count)} 条流水`;
    option.append(describe(groupName(group), groupDetail(group)), count);
    option.addEventListener("click", () => selectGroup(group));
    return option;
  }));
  model.group = listing.items.find((group) => group.group_key === model.group?.group_key) ?? model.group;
  updateGroupSelection();
  $("group-status").textContent = listing.items.length ? "" : model.groupQuery ? "未找到匹配的会话。" : "暂无会话。";
  const totalPages = Math.max(1, Math.ceil(listing.total / listing.size));
  $("group-pager").hidden = totalPages <= 1;
  $("group-page-label").textContent = `第 ${listing.page} / ${totalPages} 页`;
  $("group-previous").disabled = listing.page <= 1;
  $("group-next").disabled = listing.page >= totalPages;
}

async function loadGroups() {
  const current = ++groupsRequest;
  $("group-list").setAttribute("aria-busy", "true");
  $("group-status").textContent = "正在读取会话…";
  try {
    const listing = await bridge.apiGet("ledger/groups", { page: model.groupPage, size: 10, q: model.groupQuery });
    if (current !== groupsRequest) return;
    const lastPage = Math.max(1, Math.ceil(listing.total / listing.size));
    if (listing.page > lastPage) {
      model.groupPage = lastPage;
      await loadGroups();
      return;
    }
    renderGroups(listing);
  } catch {
    if (current !== groupsRequest) return;
    $("group-status").textContent = "会话读取失败，请刷新重试。";
  } finally {
    if (current === groupsRequest) $("group-list").setAttribute("aria-busy", "false");
  }
}

function openGroupAlias() {
  const group = model.group;
  if (!group?.group_id || aliasBusy) return;
  aliasTarget = group;
  $("group-dialog-details").textContent = `群号 ${group.group_id}`;
  $("group-alias").value = group.group_alias || "";
  $("group-alias").placeholder = group.reported_group_name || `群 ${group.group_id}`;
  $("group-dialog").returnValue = "";
  $("group-dialog").showModal();
}

async function saveGroupAlias() {
  const group = aliasTarget;
  aliasTarget = null;
  if ($("group-dialog").returnValue !== "save" || !group || aliasBusy) return;
  aliasBusy = $("edit-group").disabled = true;
  feedback("正在保存群名…");
  try {
    const groupKey = group.group_key;
    const updated = await bridge.apiPost("ledger/group-alias", {
      group_key: groupKey, alias: $("group-alias").value.trim(),
    });
    if (model.group?.group_key === groupKey) {
      model.group = { ...model.group, ...updated };
      updateGroupSelection();
    }
    feedback("已保存群名。");
    await Promise.all([loadGroups(), loadListing()]);
  } catch (error) {
    feedback(error?.message || "保存群名失败，请重试。", true);
  } finally {
    aliasBusy = $("edit-group").disabled = false;
  }
}

async function loadOverview() {
  const current = ++overviewRequest;
  try {
    const data = await bridge.apiGet("ledger/overview");
    if (current !== overviewRequest) return;
    model.timezone = data.timezone;
    $("today").textContent = `${data.date} · ${data.timezone}`;
    $("limit").textContent = `每日可换 ${text.format(data.daily_limit)} 次`;
    for (const [id, field] of Object.entries({ "today-draws": "today_draws", marriages: "marriages", draws: "draws", players: "players" })) {
      $("stat-" + id).textContent = text.format(data[field] ?? 0);
    }
  } catch (error) {
    if (current === overviewRequest) feedback(error?.message || "读取概览失败。", true);
  }
}

function selectView(view, player = null) {
  model.view = view;
  model.player = player;
  clearFilters(false);
  updateTabs();
  void loadListing();
}

function clearFilters(reload = true) {
  $("query").value = model.q = "";
  $("action").value = model.action = "";
  model.page = 1;
  feedback();
  if (reload) {
    model.player = null;
    updateTabs();
    void loadListing();
  }
}

function openReset(player) {
  if (storeBusy) return;
  resetTarget = player;
  $("reset-details").textContent = `将「${playerName(player)}」在此记录范围内的今日更换次数归零。`;
  $("reset-dialog").returnValue = "";
  $("reset-dialog").showModal();
}

async function resetCount() {
  const player = resetTarget;
  resetTarget = null;
  if ($("reset-dialog").returnValue !== "reset" || !player || storeBusy) return;
  storeBusy = true;
  feedback("正在重置今日更换次数…");
  try {
    await bridge.apiPost("ledger/reset-count", { player_key: player.key });
    feedback(`已重置「${playerName(player)}」的今日更换次数。`);
    await loadListing();
  } catch (error) {
    feedback(error?.message || "重置失败，请刷新重试。", true);
  } finally {
    storeBusy = false;
  }
}

async function start() {
  if (!bridge) throw new Error("请从 AstrBot 控制台打开抽取账本。");
  await bridge.ready();
  updateTabs();
  $("history-tab").addEventListener("click", () => selectView("history"));
  $("players-tab").addEventListener("click", () => selectView("players"));
  $("refresh").addEventListener("click", async () => {
    feedback();
    await Promise.all([loadGroups(), loadOverview().then(loadListing)]);
  });
  $("all-groups").addEventListener("click", () => selectGroup(null));
  $("edit-group").addEventListener("click", openGroupAlias);
  $("cancel-group").addEventListener("click", () => $("group-dialog").close());
  $("group-dialog").addEventListener("close", saveGroupAlias);
  $("group-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    model.groupQuery = $("group-query").value.trim();
    model.groupPage = 1;
    void loadGroups();
  });
  for (const [id, delta] of [["group-previous", -1], ["group-next", 1]]) {
    $(id).addEventListener("click", () => {
      model.groupPage += delta;
      void loadGroups();
    });
  }
  $("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    model.q = $("query").value.trim();
    model.action = $("action").value;
    model.page = 1;
    feedback();
    void loadListing();
  });
  $("clear-filters").addEventListener("click", () => clearFilters());
  for (const [id, delta] of [["previous", -1], ["next", 1]]) {
    $(id).addEventListener("click", () => {
      model.page += delta;
      feedback();
      void loadListing();
    });
  }
  $("reset-dialog").addEventListener("close", resetCount);
  await Promise.all([loadGroups(), loadOverview().then(loadListing)]);
}

start().catch((error) => {
  $("empty").textContent = "无法打开抽取账本。";
  $("records").setAttribute("aria-busy", "false");
  feedback(error?.message || "无法连接 AstrBot 管理面板。", true);
});
