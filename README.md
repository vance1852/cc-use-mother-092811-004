# 守住山区慢火车公共服务承诺协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上，项目内置了山区慢火车公共服务运行图管理：把站点服务承诺、赶集与就医日历、客货混装能力、检修封锁、补贴协议和临时灾害限制纳入同一版运行图计划。草案经地方确认（reviewer）后才能生效；每次增停、越站、取消、替代运输都记录受影响人群；临时变更必须保住已承诺的最低频次和已接收货物（不可抗力下强制变更会标记留痕）；取消与恢复都生成新事实而不删除历史。查询接口可按任意日期还原采用的运行图、未满足承诺与补救责任；考核人员（auditor）可冻结证据并复算服务覆盖，迟到客流只进入下一轮评估。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `timetable_domain.py`：运行图还原、承诺核对与考核的纯函数（不接触数据库，保证冻结证据可复算）；
  - `timetable_service.py`：运行图版本、临时变更、货物接收、客流与评估轮次的写入与查询；
  - `timetable_acceptance.py`：运行图管理的离线端到端验收；
- `tests/`：基础规则、事务边界、接口路由、运行图规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
PYTHONPATH=src python3 -m transport_coordination.timetable_acceptance
```

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链；运行图验收还会走通"承诺登记 → 草案 → 地方确认 → 生效 → 货物接收 → 替代运输与越站 → 灾害停运与恢复 → 证据冻结复算 → 迟到客流轮次"的完整链路。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

运行图接口统一挂在 `/timetable/` 前缀下：

- 事实登记：`POST /timetable/commitments`、`/calendar-entries`、`/calendar-retractions`、`/blockades`、`/restrictions`、`/subsidy-agreements`、`/goods-acceptances`、`/ridership-observations`；
- 版本流转：`POST /timetable/plans`、`/plan-confirmations`、`/plan-activations`；
- 临时变更：`POST /timetable/changes`（`change_type` 取 `add_stop`/`skip_stop`/`cancel_train`/`replacement`/`restore`，必填 `affected_groups`）；
- 考核：`POST /timetable/coverage-freezes`、`/evaluation-rounds`；
- 查询：`GET /timetable/adopted?site_id=&date=`、`GET /timetable/unmet?site_id=&date=`、`GET /timetable/changes?site_id=&date=`、`GET /timetable/coverage-freezes/{id}`、`GET /timetable/coverage-freezes/{id}/recompute`、`GET /timetable/evaluation-rounds/{id}`。
