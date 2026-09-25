# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计、通知和监管上报版本，以及跨境维修许可的额度凭证化占用、核销与释放。

代码按业务拆为三个文件：

- `store.py`：持久化（SQLite 表结构与原子读写）。
- `permits.py`：许可判定（签发、占用、核销/释放、冲突凭证与余量）。
- `app.py`：入口（HTTP 路由、召回与维修流程编排）。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。

## 许可凭证（可核销额度）

监管按 **召回 + 车型 + 方案版本 + 国家** 签发额度凭证；网点跨境报修必须引用凭证编号，报修时原子占用一笔：

- 同一凭证并发占用以条件 UPDATE 加占、`(凭证,请求键)` 唯一约束兜底，并发只放行到额度为止；同一请求键重复提交按幂等返回同一笔。
- 占用前依次校验：召回、车型、方案版本（换版即拒）、国家（与网点所在国一致）、吊销、过期、余额。拒绝返回 HTTP 409，体含 `reason`、冲突凭证（`code`/`permit_id` 等）和 `remaining` 余量。
- 维修复核通过 → 占用**核销**（额度不退回）；复核退回（flagged）或网点撤回 → **释放**额度。占用流水（held/written_off/released）永久保留。

接口：

- `POST /api/permits`：监管签发凭证（`code, recall_id, model, country, quota, remedy_version?, expires_at? 或 valid_days?`）。
- `POST /api/permits/{code}/occupy`：网点占用（`recall_id, vin, dealer_id, request_key, amount?`）。
- `POST /api/occupations/{id}/release`：释放占用（`reason?`）。
- `POST /api/permits/{code}/revoke`：吊销凭证（冻结后续占用）。
- `GET /api/permits`（可选 `?recall_id=`）、`GET /api/permits/{code}`（含占用流水）、`GET /api/permits/{code}/balance`：列表/明细/余额。
- `POST /api/repairs/{id}/withdraw`：网点撤回待复核维修（退零件并释放占用）。
- `POST /api/recalls/{id}/remedy`：修复方案换版（旧版凭证对新版维修占用即被拒）。
- 跨境报修 `POST /api/repairs` 的 `border_permit` 字段传凭证编号，自动占用并与维修单关联。

## 其他主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/scope`：调整召回范围并生成新版本通知/上报。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。首页可登记许可、占用、释放和查余额。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可凭证模型模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
