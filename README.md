# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计、通知和监管上报版本，以及**跨境维修许可的额度凭证化**。

## 代码分层（三个业务文件）

- `store.py`：持久化。SQLite 表结构、串行化写事务（`BEGIN IMMEDIATE` + 进程内锁）、审计日志。
- `permit_service.py`：许可判定。签发额度、占用、核销、释放、撤回与余额查询；过期/国家/车型/方案换版/余量校验都在这里。
- `app.py`：入口。HTTP 路由、召回与维修流程编排（`RecallService`），维修流程在同一事务内调用许可判定。

## 许可凭证模型

监管按 **召回 + 车型 + 修复方案版本 + 国家** 签发许可额度（`permits`），网点每次跨境报修占用一笔凭证（`permit_usages`）：

- **并发安全**：占用在串行立即写事务内执行 `remaining=remaining-1 WHERE remaining>0`，同一许可并发只放行总额度笔；同一引用键（报修幂等键）重复占用只放行一笔，冲突时返回已有凭证（`conflict_usage`）与余量。
- **拒绝条件**：许可过期、许可国家与网点国家不符、车型与报修车辆不符、召回方案已换版、余量不足、许可撤回——返回 409，并在响应体中带回冲突凭证快照、`remaining`、占用数与最近流水 `usage_history`。
- **核销 / 释放**：监管复核通过即核销（`written_off`，不回补余量）；复核打回（`return`）或网点撤回维修（`withdraw`）释放并回补余量；监管撤回许可（`revoke`）释放其全部占用。所有 `permit_usages` 记录始终保留。
- 跨境判定：维修网点国家与车辆**原籍国**不同即跨境，必须携带 `border_permit`（许可编号）。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。根路径 `/` 为许可登记 / 占用 / 释放 / 余额查询操作页。

## 主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/scope`：调整召回范围并生成新版本通知/上报。
- `POST /api/recalls/{id}/remedy`：修复方案换版（旧版许可之后占用将被拒绝）。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修（跨境报修在同事务内占用许可）。
- `POST /api/repairs/{id}/withdraw`：网点撤回待复核维修，释放凭证与零件。
- `POST /api/permits`：监管签发许可额度；`POST /api/permits/{code}/revoke`：撤回许可。
- `POST /api/permits/{code}/occupy`：网点单独占用一笔凭证（`ref_key`、`vin`、`dealer_id`）。
- `POST /api/permit-usages/{id}/release`：释放凭证（`reason=return|withdraw`）。
- `GET /api/permits`：许可清单；`GET /api/permits/{code}`：余额、占用/核销/释放计数与最近流水。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_permits.py` 覆盖并发占用（线程池）、过期 / 国家 / 车型 / 方案换版 / 余量不足拒绝、核销与释放、撤回释放、跨境维修同事务占用与打回退回。

当前为本地原型：跨境规则以许可凭证为模型，零件库存与维修记录是简化实现，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
