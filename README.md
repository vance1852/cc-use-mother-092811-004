# 守住山区慢火车公共服务承诺协同基础服务

本项目在综合交通运输共享基础能力（运营机构、交通节点、操作者、角色权限、请求幂等、
SQLite 事务与哈希串联审计）之上，提供一套完整的**山区慢火车公共服务运行图管理系统**。

它回应这样一种矛盾：客流数据建议压缩低利用停站，而沿线乡镇依靠这些车次赶集、就医和
运送小件农货。系统不以平均人数简单裁撤，而是把以下内容纳入同一版计划：

- **站点服务承诺**：站点、依赖人群（赶集商贩、就医群众、学生、留守老人等）；
- **赶集与就医日历**：每周规则、固定日期、每月固定日、第 N 个星期等日历规则；
- **客货混装能力**：每列车的货物载重与停站时刻序列；
- **检修封锁与临时灾害限制**：站点级、区段级，区分压停与中断；
- **补贴协议**：承诺挂接协议，未满足时给出补救责任方；
- **临时变更**：增停、越站、取消、恢复，全部是只追加的事实，明确影响了哪些人群；
- **替代运输**：公路班车/应急车兜底，可挂接某次变更并指定承运已接收货物。

## 核心规则

1. 运行图以**草案**创建，挂接列车与停站，经地方确认（reviewer 角色）后才**生效**；
   生效后基准内容不可改，运行期变化只能以临时事实表达。
2. 每次临时变更都计算并记录**受影响人群**；取消与恢复都生成新事实，历史不删除。
3. 临时变化必须保住已承诺的**最低频次**（需求日班次、每周班次）与**已接收货物**：
   未满足时按日期列出缺口，并依据补贴协议给出补救责任方。
4. 查询接口可按**任意日期**还原当天采用的运行图、实际停站、替代运输、未满足承诺与货物去向。
5. 客流评估分**轮次**：轮次冻结时把当时的计划事实与客流观测固化为带哈希的证据快照，
   考核人员可用同一快照**确定性复算**服务覆盖；冻结后迟到的客流只能进入下一轮评估。

## 目录

- `src/transport_coordination/`
  - `service.py` / `storage.py` / `audit.py`：基础登记、权限、幂等、SQLite、哈希审计链；
  - `coverage.py`：日历激活、当日有效运行图、承诺/货物核算、冻结复算等纯计算引擎；
  - `public.py`：公共服务运行图领域服务；
  - `api.py`：不依赖第三方框架的 HTTP/JSON 边界；
  - `acceptance.py` / `public_acceptance.py`：基础链与公共服务运行图离线验收。
- `tests/`：基础规则、纯计算引擎、公共服务规则、HTTP 路由与端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.public_acceptance
```

公共服务验收会在临时库中演示：站点与日历、补贴协议与承诺、草案确认、赶集日收货、
灾害区段压停与公路兜底、列车取消但已接收货物由替代运输承运、恢复开行产生新事实、
赶集日越站的承诺缺口与责任方、客流轮次冻结复算，以及迟到客流进入下一轮评估。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite
中的业务状态、审计历史与冻结证据继续保留。

主要公共服务接口（均在 `/public` 前缀下，写接口需带 `request_id` 以保证幂等）：

| 方法与路径 | 作用 |
| --- | --- |
| `POST /public/stations` | 登记站点与依赖人群 |
| `POST /public/calendars` | 登记赶集/就医日历 |
| `POST /public/agreements` | 登记补贴协议与补救责任方 |
| `POST /public/commitments` | 挂接站点最低服务承诺到协议 |
| `POST /public/plans` | 创建草案版运行图（可声明替代旧版） |
| `POST /public/trains` | 在草案中增加列车、停站与货载能力 |
| `POST /public/blocks` `/public/disasters` | 检修封锁、灾害限制 |
| `POST /public/plans/confirm` | 地方确认，固化快照与证据哈希 |
| `POST /public/consignments` `/consignments/carried` | 接收/承运小件农货 |
| `POST /public/amendments/add-stop` `/skip-stop` `/cancel-train` `/restore` | 临时变更事实 |
| `POST /public/replacements` | 安排替代运输并指定承运货物 |
| `POST /public/rounds` `/rounds/freeze` `/ridership` | 客流评估轮次与观测 |
| `GET /public/plan-for-date?site_id=&date=` | 查询某日采用的生效计划 |
| `GET /public/day-report?site_id=&date=` | 还原当日运行图、承诺缺口与补救责任 |
| `GET /public/snapshot?plan_id=` | 读取计划事实快照 |
| `GET /public/amendments?plan_id=` | 查看只追加的变更历史 |
| `GET /public/round-evidence?round_id=` | 读取冻结证据并复算覆盖摘要 |
