# 机房配线库 · 误删链路恢复（Flask + 双 SQLite）

值班员从**备份库**勾选误删的链路，系统按 **设备资产号 + 端口名** 到**现库**匹配两端端口记录，
预览现库端口信息、占用情况以及不能补回的原因；确认时进行乐观一致性校验并**整批原子写入**。

- 备份库与现库各是一份独立 SQLite：`data/backup.db`、`data/live.db`
- 备份记录号（`links.id`）**可能已被现库新链路复用**，因此绝不按记录号匹配
- 每个端口最多接一条链路：在现库任何链路的 **A 端或 B 端**出现都算占用
- 预览后现库可照常写入；只有涉及所选端口 / 所列记录的变化会在确认时被拦下，**无关改动不影响确认**
- 确认在 `BEGIN IMMEDIATE` 事务内复查：端口记录与预览快照一致 **且** 全部空闲，才整批插入；
  否则逐条指出原因并回滚，**整批不写入**

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

# 现库常规写入
curl -s localhost:8000/api/live/links
curl -s localhost:8000/api/live/links -H 'Content-Type: application/json' \
  -d '{"a_asset":"ZC-PP-11","a_port":"P07","b_asset":"ZC-PP-11","b_port":"P08"}'
curl -s -X DELETE localhost:8000/api/live/links/9
curl -s -X PATCH localhost:8000/api/live/ports/3 \
  -H 'Content-Type: application/json' -d '{"remark":"新备注"}'

curl -s -X POST localhost:8000/api/demo/reseed
```

确认冲突时返回 `409`，体中含每条链路的 `reasons` 以及可直接再次使用的 `fresh_token`；
token 无效 / 过期返回 `400`。

## 目录结构

```
.
├── app/
│   ├── __init__.py      # Flask 工厂、健康检查、错误页
│   ├── db.py            # SQLite 连接、schema、占用触发器
│   ├── core.py          # 匹配 / 预览快照 / 原子确认 / 现库写入
│   ├── tokens.py        # 预览快照签名 token
│   ├── routes.py        # HTML 路由 + JSON API
│   ├── seed.py          # 演示数据（python -m app.seed）
│   ├── templates/       # base/recovery/preview/result/live/error
│   └── static/style.css
├── tests/test_recovery.py
├── data/                # backup.db / live.db（运行时生成，compose 卷挂载）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── wsgi.py              # gunicorn wsgi:app / python wsgi.py
```
