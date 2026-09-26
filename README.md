# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项（会签清单项）。

## 会签与投产流程

1. **评估（assess）**：提交`risk_level`、`analyst`和`checklist`。清单每项必须包含`description`、`owner`（负责人）和`due_date`（完成时间）；评估通过后系统为每项生成一条`action_item`，id记录在变更的`checklist_item_ids`中。
2. **逐项签认（claim → sign）**：审核人（safety/engineer）先`claim`认领自己的项，再`sign`签认；未认领的项不能签认。
3. **审批（approve）**：不再按人数判断。清单为空或存在未签认项时直接退回，错误信息逐项列出未完成的item id。
4. **实施（implement）**。
5. **投产前安全再确认（reconfirm）**：变更批准后装置只要发生过停机或冻结（即使已重启/解冻），或当前处于冻结，投产前必须由safety岗位重新确认控制措施（`{"controls_confirmed": true}`）。当前处于停机状态则必须先恢复运行。再确认晚于最近一次停机/冻结才有效；`reconfirm`幂等，重复提交直接返回已有记录，不新增版本和审计行。
6. **投产（commission）**：所有行动项verified，且再确认要求满足后才能投产。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
