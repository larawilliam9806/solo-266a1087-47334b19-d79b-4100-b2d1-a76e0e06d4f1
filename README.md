# 机房配线库 · 误删链路恢复（Flask + 双 SQLite）

值班员从**备份库**勾选误删的链路，系统按 **设备资产号 + 端口名** 到**现库**匹配两端端口记录，
预览现库端口信息、占用情况以及不能补回的原因；确认时进行乐观一致性校验并**整批原子写入**。

- 备份库与现库各是一份独立 SQLite：`data/backup.db`、`data/live.db`
- 备份记录号（`links.id`）**可能已被现库新链路复用**，因此绝不按记录号匹配
- 每个端口最多接一条链路：在现库任何链路的 **A 端或 B 端**出现都算占用
- 预览后现库可照常写入；只有涉及所选端口 / 所列记录的变化会在确认时被拦下，**无关改动不影响确认**
- 确认在 `BEGIN IMMEDIATE` 事务内复查：端口记录与预览快照一致 **且** 全部空闲，才整批插入；
  否则逐条指出原因并回滚，**整批不写入**
- **接线历史 `/history`**：选一个端口和两个变更编号，查看起始编号提交后的对端 / 空闲状态，
  以及之后每笔接线、拆线及对端；拆了又接回原对端会分成两条显示。首次启用为已有接线记 `#1`
  起始编号，重启不丢；网页 / API 手工增删各一笔，整批补回共用一笔；接线与历史同事务提交。
  重置演示数据会另开一个历史段，旧编号不会指向新记录。

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

打开 <http://localhost:8000>（同时映射了 <http://localhost:5000>）。
首次启动若 `./data` 下没有数据库，会自动生成覆盖各类冲突场景的演示数据。

> 如需固定签名密钥，启动前设置环境变量：`SECRET_KEY=your-key docker compose up --build`

### 方式二：本地 Python 运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python -m app.seed     # 生成 data/backup.db 与 data/live.db（已存在则跳过）
python wsgi.py         # 默认 http://0.0.0.0:5000 ，可用 PORT=8000 覆盖
```

运行测试：

```bash
pip install pytest
python -m pytest tests/ -q
```

## 页面操作流程

1. **备份恢复页 `/`**：默认列出“现库已无相同两端链路”的疑似误删记录，可切到“全部备份记录”。
   表中会标出“该记录号现库已被复用”。
2. 勾选若干链路 → **预览所选链路**。
3. **预览页**对每条链路展示：
   - A/B 两端对应的**现库端口记录**（现库端口记录号、资产号、设备名称/型号/位置、端口名、类型速率、状态、备注）；
   - 端口是否空闲；若被占用，指出占用它的现库链路编号、两端以及该端口出现在该链路的哪一端；
   - 不能补回的全部原因（设备/端口不存在、端口被占用、同批重复选中等）；
   - 备份记录号被现库不同链路复用时的明确提示。
4. 等待期间可在 **现库链路 / 端口页 `/live`** 正常新增、删除链路或修改端口备注。
5. 回到预览页点 **确认整批补回**：
   - 全部通过 → 结果页列出补回链路及其在现库的**新记录号**（不沿用可能已复用的备份旧号）；
   - 有冲突 → 页面顶部列出每条链路的冲突原因，数据库零写入，并给出按当前现库刷新的预览与新凭证。

顶部“重置演示数据”可随时把两份库恢复到初始场景（JSON API 为 `POST /api/demo/reseed`）。

## 接线历史与掉线排查（`/history`）

顶部导航点 **接线历史**（也可用 `/wiring-history`、`/live/history`）：

1. **选择端口**：下拉列出全部现库端口及当前空闲 / 占用状态；现库页每个端口行也有“接线历史”直达链接。
2. 下拉选择 **起始变更编号** 与 **结束变更编号**，每项都显示 `#编号 · 提交时间 · 类型`。
3. 页面先给出 **起始编号提交后** 该端口的状态：
   - 空闲 → 绿色“空闲”标记；
   - 已接线 → 显示 **对端资产号/端口名** 与链路编号。
4. 然后列出 **从下一笔（起始编号 +1）到结束编号** 之间，与该端口相关的每次 **接线 / 拆线**：
   变更编号、提交时间、操作徽标、本端 A/B 侧、**对端**、链路编号、来源。
   拆线后又接回同一个对端，是 `拆线`、`接线` 两条独立记录，不会合并。
5. 页面下方表格列出当前历史段的全部 **变更编号与提交时间**、类型、本笔改动链路数与渠道。

编号是**历史段内从 1 开始的连续整数**：

- **首次启用**（旧库升级或全新播种）会把当时已有的 5 条接线整体记为 `#1 起始基线`；
- 一次网页表单或 JSON API 的手工新增 / 删除各产生一个新编号（`manual_add` / `manual_delete`）；
- 备份**整批补回共用一个编号**（`restore_batch`），编号下为本次补回的 N 条链路各写一行增量；
- 接线写入与历史明细在**同一个 `BEGIN IMMEDIATE` 事务**里提交；校验冲突 / 约束失败 / 历史写入异常
  都会整体回滚，**失败不留任何历史编号**；
- 每个编号只写 1 行 `change_revisions` + 本次改动链路数行 `link_changes`，
  因此每笔新增历史的数据量是 O(本次改动链路数)，不随全库接线总量膨胀；
- 数据库文件持久化在 `data/live.db`（compose 卷挂载），**重启进程 / 容器后仍按原编号查询**；
- 读历史走独立连接，SQLite 启用 WAL，**查询期间现库仍可正常提交接线变更**。

**重置演示数据后另开一段历史**：`POST /api/demo/reseed`（或页面顶部“重置演示数据”）会重建
live.db 并生成新的 `segment_id`，段内编号重新从 `#1` 基线开始。用新段查旧的大号会得到
`404 revision_not_found`；携带旧 `segment_id` 查询会得到 `410 history_segment_gone`，
旧编号绝不指向新段记录。

### 接线历史 JSON API

```bash
# 当前历史段 + 编号列表 + 端口列表（不带端口参数时为总览）
curl -s localhost:8000/api/history
curl -s localhost:8000/api/history/revisions

# 按端口记录号查询：起始 #1 提交后的状态 + #2..#4 的接线/拆线
curl -s "localhost:8000/api/history?port_id=15&start=1&end=4"
# 等价写法：from/to、from_rev/to_rev，或按坐标（端口名自身可含 /，只切第一个 /）
curl -s "localhost:8000/api/history?port=ZC-PP-11/P07&from=1&to=4"
curl -s "localhost:8000/api/history?asset_no=ZC-HX-01&port_name=GE1/0/1&start=1&end=1"
# 路径风格与 POST 风格
curl -s localhost:8000/api/history/port/15/1/4
curl -s -X POST localhost:8000/api/history/query \
  -H 'Content-Type: application/json' -d '{"port_id":15,"start":1,"end":4}'

# 某一笔变更改了哪几条链路
curl -s localhost:8000/api/history/revisions/2
# 历史段
curl -s localhost:8000/api/history/segments
```

查询响应中：

- `state_at_start`（别名 `start_state` / `from_state` / `baseline_state`）：
  `{state:"idle"|"connected", occupied, idle, label, peer:{coord,...}|null, peer_coord, ...}`；
- `events`（别名 `changes` / `history` / `timeline` / `records`）：
  每项含 `rev_no`/`number`、`committed_at`、`action`（`connect`/`disconnect`）、
  `action_label`（`接线`/`拆线`）、`peer_coord`、`peer`、`link_id`、`link_no` 等；
- 同时返回 `segment_id` / `epoch_id`，重置后该标识会变化。

兼容别名：页面 `/wiring-history`、`/live/history`；接口前缀 `/api/wiring-history`、
`/api/live/history`；编号列表 `/api/revisions`、`/api/changes`。

## 内置演示场景

| 备份记录 | 两端坐标 | 设计好的情形 |
|---|---|---|
| `#1` `#2` | 与现库 `#1` `#2` 完全相同 | 链路仍在现库，不算误删 |
| `#3` | `ZC-PP-11/P03` ↔ `ZC-HX-01/GE1/0/2` | 两端空闲，可补回 |
| `#4` | `ZC-PP-11/P04` ↔ `ZC-HX-01/GE1/0/3` | A 端已被现库链路 `#4` 占用 |
| `#5` | `ZC-PP-12/P03` ↔ `ZC-CK-03/GE0/0/1` | B 端已被现库链路 `#5` 占用 |
| `#6` | `ZC-OLD-99/F0/1` ↔ `ZC-HX-01/GE1/0/24` | 资产 `ZC-OLD-99` 已在现库下架，匹配不到 |
| `#7` | `ZC-PP-11/P05` ↔ `ZC-FWQ-21/eth1` | **记录号 7 已被现库另一条链路 `#7` 复用**，但按坐标仍可匹配、可补回 |
| `#8` `#9` | 见备份库 | 另外两条可补回链路，便于演示整批成功 / 原子失败 |

## 并发与一致性是怎么保证的

- 预览时，服务端把每个端点匹配到的**现库端口记录完整字段**生成快照，连同所选备份 id、
  备份两端坐标与属性指纹，用 `itsdangerous` 签名成 token 放在隐藏字段里（不引入第三份库）。
- 确认时在**单个 SQLite 事务**内执行 `BEGIN IMMEDIATE`：
  1. 重新按资产号 + 端口名匹配并读取最新记录；
  2. 与预览快照逐字段比对（记录被删 / 重建导致 id 变化 / 字段被改 / 预览后新增，都会报“与预览不一致”）；
  3. 检查端口此刻是否被任何链路在 A/B 任一端占用；
  4. 检查同批内部是否重复使用同一端口；
  5. 全部通过才插入全部 links 并 `COMMIT`，否则 `ROLLBACK`。
- `busy_timeout=30s` 让并发确认 / 现库写入排队；写事务串行，两个竞争批次至多一个成功。
- 数据库还装有跨 A/B 端的占用触发器作为最后防线，即使绕过应用直接写 SQLite 也不能把同一端口放进两条链路。

## JSON API（语义与页面一致）

```bash
# 预览（返回签名 token 与逐项原因）
curl -s localhost:8000/api/preview \
  -H 'Content-Type: application/json' \
  -d '{"link_ids":[3,7,8]}'

# 用预览 token 确认（推荐，带快照一致性校验）
curl -s localhost:8000/api/confirm \
  -H 'Content-Type: application/json' \
  -d '{"token":"<token>"}'

# 也允许不带 token，直接按确认瞬间的现库状态整批校验/写入
curl -s localhost:8000/api/confirm \
  -H 'Content-Type: application/json' \
  -d '{"link_ids":[3]}'

# 现库常规写入（成功响应额外带 revision 变更编号信息；POST /api/live/links/add 为等价别名）
curl -s localhost:8000/api/live/links
curl -s localhost:8000/api/live/links -H 'Content-Type: application/json' \
  -d '{"a_asset":"ZC-PP-11","a_port":"P07","b_asset":"ZC-PP-11","b_port":"P08"}'
curl -s -X DELETE localhost:8000/api/live/links/9
curl -s -X PATCH localhost:8000/api/live/ports/3 \
  -H 'Content-Type: application/json' -d '{"remark":"新备注"}'

# 接线历史（详见上文“接线历史与掉线排查”）
curl -s "localhost:8000/api/history?port_id=15&start=1&end=4"

curl -s -X POST localhost:8000/api/demo/reseed
```

确认冲突时返回 `409`，体中含每条链路的 `reasons` 以及可直接再次使用的 `fresh_token`；
token 无效 / 过期返回 `400`。

## 目录结构

```
.
├── app/
│   ├── __init__.py      # Flask 工厂、健康检查、错误页、历史引导
│   ├── db.py            # SQLite 连接、schema、占用触发器、历史表
│   ├── core.py          # 匹配 / 预览快照 / 原子确认 / 现库写入（同事务记历史）
│   ├── history.py       # 历史段 / 变更编号 / 增量明细 / 端口区间回放
│   ├── tokens.py        # 预览快照签名 token
│   ├── routes.py        # HTML 路由 + JSON API（含 /history 与 /api/history）
│   ├── seed.py          # 演示数据（python -m app.seed）
│   ├── templates/       # base/recovery/preview/result/live/history/error
│   └── static/style.css
├── tests/test_recovery.py
├── tests/test_history.py
├── data/                # backup.db / live.db（运行时生成，compose 卷挂载）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── wsgi.py              # gunicorn wsgi:app / python wsgi.py
```
