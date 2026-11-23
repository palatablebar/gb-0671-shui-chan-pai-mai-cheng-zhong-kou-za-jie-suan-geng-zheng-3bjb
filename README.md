# 东港水产拍卖结算

给水产市场结算员处理双次称重、扣杂计价、结算更正、应付复核与按户净额划转。

八个模块：称重采信、货品换算、毛皮配对、扣杂竞价、更正裁决、应付对账、主体净额、结算归档。每个导航页均可重跑同一批数据，隔离行保留编号、原值、行号和原因码。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

浏览器访问 http://localhost:18240 。默认载入现场脏批，包含53条逻辑输入、重复表头、缺列、空值、重复冲突、缺皮重、毛重小于皮重等问题。切换“核验清批”或“旧编码批”后重跑。进入“更正裁决”核对票00001：原应付1168.50CNY，更正后1285.35CNY，已付1168.50CNY，仅待付116.85CNY。

### 主体净额（先出令还是等确认）

同一主体既收货款又付船款时，按户轧差生成可追溯净额指令。默认口径 `closed_first`（已闭合票先出令）：只纳入已闭合票与已确认船款，未闭合票逐张列入暂缓，迟到更正只替换对应票并生成新指令版本，不会重复支付或重复累加净额。切换 `all_confirmed`（全票确认后轧差）时，未闭合的主体整户暂缓，已核对主体照常出令。两种口径都可用 `policy` 参数指定：`POST /api/runs`、`POST /api/runs/{run_id}/rerun`、`POST /api/netting` 或页面 `/netting?policy=`。

暂缓条件：主体身份冲突（同票登记到多个主体）、船款登记冲突、更正未闭合（`pending_corrections` 或版本冲突）、同票金额无法对平（应付≠已付+待付，整户暂缓）、隔离行、净额与归档核对不一致（整批暂缓）。指令台账按谱系保留逐版指纹与 `supersedes`，暂缓版只入台账不顶替上一版；同一指纹重跑不新增版本、不累加净额。`GET /api/runs/{run_id}/commands` 查看台账，`GET /api/export/{run_id}` 的 `netting.json` 与 `source.csv` 保留指令依据和原始凭证。

API：各模块 `POST /api/<导航英文名>`；完整运行 `POST /api/runs`，JSON参数可用 `dataset` 或 `csv_text`。查询历史 `GET /api/runs`，复核 `POST /api/runs/{run_id}/review`，传入当前 `revision`。封存下载只有 `GET /api/export/{run_id}` 一个入口。

同输入和规则摘要生成相同批号，不新增重复记录；复核版本改变后旧版本提交返回409。SQLite保存在命名卷。可使用新卷名称重建演示数据，原卷保留。

定义见 `docs/DATA-DICTIONARY.md`，模块与测试映射见 `docs/DEPTH.md`。源码在 `app/`，业务样例在 `sample_data/`，测试在 `tests/`。不连接实际秤台和支付渠道。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
