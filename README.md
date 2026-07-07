# Travel Planner · 本地私人行程规划器

一个**完全本地、单用户**的旅行行程规划器，跑在自己的 Mac 上。用一个单文件网页 UI **逐日查看行程、浏览每趟的文档、就地编辑与拖拽排版（含地图、天气、航班实时信息）**；也能把 `~/Documents/Travel Plan/` 里的旧行程文档导入成结构化 JSON。`data/` 下的结构化 JSON 是 going-forward 的**唯一真源**——不再需要用 Apple Pages 编辑行程。

> 无构建步骤、无包管理器、无必配的 key。架构 = 标准库 `python3` 的 `ThreadingHTTPServer` + 一个 `index.html`（原生 JS + 两个 CDN 库）。唯一的 LLM 是**可选**的本地 `claude` CLI——没有也能完整使用，见[「没有 Claude Code 会怎样」](#-没有-claude-code订阅会怎样)。

## 🚀 上手指南

### 第 1 步 · 电脑端（Mac）

**0. 要求**：macOS（自带 `python3` 即可）。不用装任何依赖包；下面的 key 与工具**全部可选**。

**1. 跑起来**

```bash
git clone <这个仓库> && cd travel-planner
python3 server.py        # → http://localhost:8787
```

- 改 `index.html` 或 `data/` 下的 JSON 只需**刷新浏览器**；**只有改 `server.py`（或 `import/common.py`）才需要重启**。
- 端口固定 8787；默认只监听 `127.0.0.1`——本服务**无鉴权**，这是最安全的默认。

**2. 建第一趟行程**

首页 **➕ New trip** → 名称 / 开始日期 / 天数。打开行程后**处处可就地编辑**：

- 双击标题/日期/文字直接改；悬停每行出现 ✎ 编辑、＋ 插入（行程项或两点间交通）、拖拽排序；`⌘Z` 撤销。
- 右侧地图：**🔍 搜索地点**加入行程、**点地图落点**、每行 **📍 定位**。行程有了地点后，天气卡与逐日天气自动出现（keyless 的 Open-Meteo）。
- 新建行程会在 `~/Documents/Travel Plan/` 下建一个 `YYYY:MM 标题` 文件夹；往 Documents 面板拖文件就归档进去（想换目录：环境变量 `TRAVEL_DIR`）。

**3. 配 key（可选，建议配 Google）**——项目根目录建 `config.local.json`（已 gitignore，永不入库）：

```json
{ "googleMapsApiKey": "AIza…", "aeroDataBoxKey": "…" }
```

| Key | 打开的能力 | 不配时的降级 |
|---|---|---|
| `googleMapsApiKey` | Google 地图、富地点详情卡、驾车/步行/公交路线与用时 | Leaflet + OSM 地图、OSRM 驾车用时——功能齐全，少了地点卡与公交 |
| `aeroDataBoxKey`（RapidAPI） | 航班悬停出实时状态/航站楼/登机口/机型 | 离线解析卡（航线/时刻/航司照常显示） |

Google key 在 Cloud Console 启用下文 [Google Maps Platform APIs](#%EF%B8%8F-google-maps-platform-apis逐个拆开) 表里的 **6 个 API**（个人用量全部在免费额度内，实际 ≈ $0/月），并把 referrer 白名单加上 `http://localhost:8787/*`。

**4. 装 Claude Code CLI（可选）**——已安装并登录过 [`claude`](https://claude.com/claude-code) 的话，「🗺 生成地图」「导入老文档的 LLM 清洗」「🏞 换封面的地标提名」「✨ AI 天气摘要」即开即用（直接复用你的登录态，**无需 API key、无单独计费**）。默认从 PATH 找 `claude`，可用环境变量 `CLAUDE_BIN`（二进制路径）/ `CLAUDE_MODEL`（默认 `claude-opus-4-8`）覆盖。没有它的差异见下一大节。

**5. 导入已有行程文档（可选）**——旧行程按 `YYYY:MM 标题` 命名放进 `~/Documents/Travel Plan/`（注意是英文冒号 `:`，Finder 会显示成 `/`），然后：

```bash
brew install pandoc poppler        # 解析 .pages 导出与 pdf 所需（只在导入时用）
python3 import/scan.py             # 建库：元数据 + 文档清单 + 封面占位
python3 import/extract_pages.py    # .pages 行程表 → days[]（确定性、无 LLM；需装 Pages.app，首次弹一次自动化授权）
python3 import/docparse.py         # 老 pdf/docx/xlsx → 原文文本
python3 import/structure.py --all-raw   # 原文 → days[]（需要 claude）
python3 import/geocode.py --all    # 行程文字 → 地图地点（claude 提查询 + 免费 Nominatim 解析）
python3 import/covers.py --all     # 目的地封面照（Wikipedia/Commons，免费）
```

### 第 2 步 · 手机端

1. **联通（推荐 Tailscale**——加密、免端口配置、只有你自己的设备可达）：Mac 和手机都装 [Tailscale](https://tailscale.com) 并登录同一账号，然后：

   ```bash
   HOST=0.0.0.0 python3 server.py
   ```

   启动日志会直接打印手机可用的地址（`http://<mac 的 tailscale 名>:8787`）。
   - 服务端内置 IP 白名单：`HOST=0.0.0.0` 也**只放行回环 + Tailscale 网段**，咖啡馆/机场的同网段设备一律 403。只在可信的家庭 Wi-Fi 上想不装 Tailscale 直连时，改用 `ALLOW_LAN=1 HOST=0.0.0.0 python3 server.py`（额外放行私有网段；`ipconfig getifaddr en0` 查 Mac 的局域网 IP）。
   - **切勿暴露公网**：存在 `/api/open`（在 Mac 上打开文件）、写数据、下发 Google key 的接口且**无鉴权**——放公网前务必自行加鉴权/反代。
2. **加到主屏幕**：手机浏览器打开上述地址 → 分享 → **添加到主屏幕**，之后以独立全屏 PWA 运行。
3. **手机交互对照**：桌面的悬停操作在手机上收进每行/每天/标题旁的 **⋯ 底部菜单**；**长按拖拽**排序；右下 **🗺 悬浮球**滑出全屏地图；文档点开在浏览器内 QuickLook 预览；**导出 PDF 由 Mac 上的无头 Chrome 代为渲染**（Mac 装有 Chrome/Chromium/Edge/Brave 任一即可；一台都没有则该入口自动隐藏）。
4. **手机上的 Google 地图**：key 的 referrer 白名单要**加上手机访问的地址**（如 `http://<tailscale 名>:8787/*`），否则手机上 Google 地图显示报错水印（不加也能退回 Leaflet 用）。

## 🤷 没有 Claude Code（订阅）会怎样？

**核心体验不变。** 新建/编辑行程、地图搜索与手动定位、天气（气候 + 逐日预报）、航班解析与实时信息、文档、PDF 导出、手机端——这些**全都不经过 LLM**。`claude` CLI 只在四处使用，且全部优雅降级：

| 功能 | 有 claude | 没有 claude |
|---|---|---|
| **导入老文档的清洗**（`structure.py`，非 `.pages` 的 PDF/DOCX/XLSX） | 脏文本自动重建成逐日行程 | 该行程停留在「原文可读」状态，在 UI 里手动录入；**`.pages` 导入完全不受影响**（确定性解析、无 LLM） |
| **🗺 生成地图**（`geocode.py`，从整趟行程文字批量提取地点） | 一键把全程标到地图 | 此按钮不可用；改用地图 **🔍 搜索**、每行 **📍 定位**、**点图加点**——同样能把地图建全（Nominatim/Google，均非 LLM） |
| **🏞 封面换图**的地标提名（`covers.py`） | claude 提名多个上镜地标供轮换 | 仍能换图（按行程标题搜 Wikipedia 首图）+ **📷 上传自己的封面**；也可手动维护 `data/cover_landmarks.json`（`行程id → 地标名`） |
| **✨ AI 天气摘要**（打包/注意事项建议） | 天气卡 ✨ 悬停出 2–3 句建议 | 静默省略；普通天气卡数据完整保留 |

一句话：**claude 主要服务于「把老文档批量导入」这个一次性场景**；从零开始在 UI 里建行程的用户几乎感知不到差别。另外 `claude` CLI 并非只有订阅一条路——`export ANTHROPIC_API_KEY=…` 可按量计费运行，`CLAUDE_MODEL` 可指到任何你有权限的模型。

## 数据管线（导入）

```
~/Documents/Travel Plan/<YYYY:MM 标题>/        (只读源；TRAVEL_DIR 环境变量可改)
  └─ import/scan.py           → data/trips/<id>.json + trips_index.json + 封面
  └─ import/extract_pages.py  .pages → docx → html → days[]（确定性，无 LLM）
  └─ import/docparse.py       pdf/docx/xlsx → data/raw/<id>.txt（老的非 .pages 行程）
  └─ import/structure.py      原文 → days[]   （用 claude 清洗结构化）
  └─ import/geocode.py        days[] → trip["places"]（claude 提取查询 + Nominatim 解析）
  └─ import/covers.py         地标 → Wikipedia/Commons 照片 → data/covers/<id>.jpg
                              （人工策展地标表 data/cover_landmarks.json 优先）
  └─ import/airport_tz.py     OpenFlights → data/airport_tz.json（IATA→时区表）
                              + data/airport_geo.json（IATA→经纬度，地图上航班端点用）

server.py (8787)  serve index.html + data/，外加一套小 JSON API。
```

详细的数据模型、约定与"gotcha"见 `CLAUDE.md`（开发笔记；因其示例大量引用作者的真实行程，**已 gitignore、不入库**——clone 下来没有这个文件是正常的）。

---

# 外部依赖、API 调用与免费额度

整个 repo 的外部依赖很集中：**LLM 只有一个**（本地 `claude` CLI，可选），**需要 key 的外部 API 只有两个**（Google、AeroDataBox，均可选），其余全部 **keyless**。拓扑上，前端大多只调自家 `/api/*`，由 `server.py` 再代理出站；只有 Google 与 OSRM 是前端直连。

## 🤖 LLM 使用（本地 `claude` CLI，可选）

唯一的 LLM 是本地 `claude` CLI。三处在**导入/生成阶段**做"杂乱文本 → 干净结构化知识"（封装 `import/structure.py:ask_claude()`），另有一处**浏览时**的天气摘要（封装 `server.py:_ask_claude_text()`，前端经 `/api/weather/summary` 触发、按 prompt 哈希缓存）。两个封装跑的是同一条命令：

```bash
claude -p <prompt> --model $CLAUDE_MODEL --effort low \
       --permission-mode bypassPermissions --output-format text
# 二进制与模型可用环境变量覆盖：CLAUDE_BIN（默认从 PATH 找）、CLAUDE_MODEL（默认 claude-opus-4-8）
```

- **复用你已登录的 `claude`，无 API key、无单独计费**。`--effort low`（都是机械活，够用且快），输出强制 JSON 后由 `parse_json()` 抠出对象；`geocode.py` / `covers.py` 经 `import structure` 复用同一封装。

**四处用途**：

| # | 位置（prompt） | 何时跑 | claude 干什么 | 下游（非 LLM） |
|---|---|---|---|---|
| 1 | `structure.py`（`PROMPT`） | **非 `.pages` 老行程**（PDF/DOCX/XLSX）在 `docparse.py` 出原文后；`structure.py --all-raw` 批处理 | 把最多 14k 字脏文本（拍平的表格/OCR）重建成 `days[]` JSON：逐行分类（flight/transfer/hotel/activity/meal/note）、航班码与时刻与 `+1` 原样保留、保留原文拼写（含中文）、按月份推日期 | 直接写回 `data/trips/<id>.json` |
| 2 | `geocode.py`（`EXTRACT_PROMPT`） | 生成地图：`geocode.py` / UI「🗺 生成地图」/ `POST /api/geocode` | 从每天文字挑出真实地点，并**用世界知识把名字补全成 OSM 能搜到的形式**（`Central` → `Central Restaurante, Lima, Peru`；`Goðafoss` → `Goðafoss, Iceland`），跳过机场码与 free day，≤40 个 | 清洗后的 `query` 交 **Nominatim** 查经纬度 |
| 3 | `covers.py`（`LIST_PROMPT` / `PROMPT`） | 取封面/换图：`covers.py` / UI「🏞 Change photo」/ `POST /api/cover/auto` | 为目的地命名 **N 个不同的标志性、上镜地标**（Wikipedia 条目名，最具代表性优先，换图时能在不同地点间切）；另有批量版一次映射多趟。人工策展的 `data/cover_landmarks.json` 优先，claude 只补该表没覆盖的 | 地标名交 **Wikipedia / Commons** 抓首图 |
| 4 | `server.py`（`_wx_summary_prompt`） | **浏览时**：天气卡头部 ✨ 悬停 → `POST /api/weather/summary` | 按该行程的气候卡（各地温度/雨天数）+ 活动构成，写 2–3 句**打包/注意事项建议**（纯文本） | 按 prompt 哈希缓存到 `cache/wx_summary.json`（天气/行程变了才重新生成） |

**要点**：
- `.pages` 行程**不用** claude——`extract_pages.py` 确定性地解析表格（claude 只是非 `.pages` 脏行程的清洗兜底）。
- 前三处按行程导入/生成时跑一次；第 4 处虽由浏览触发，但**有缓存**（同一行程同样的天气只生成一次），且失败时静默降级为普通天气卡。
- 全 repo 没有任何其它 AI 供应商（无 OpenAI / Gemini / Anthropic HTTP API 等）。

## 🗺️ Google Maps Platform APIs（逐个拆开）

**计费模型（2025-03-01 起）**：取消了旧的 $200/月统一额度，改为**每个 SKU 每月独立的免费额度**——
**Essentials 10,000 次 / Pro 5,000 次 / Enterprise 1,000 次**（每月、每 SKU）。
官方依据：[价格总览/SKU 价目表](https://developers.google.com/maps/billing-and-pricing/pricing) · [2025-03 变更说明](https://developers.google.com/maps/billing-and-pricing/march-2025) · [官方博客：每产品最多 10,000 免费/月](https://mapsplatform.google.com/resources/blog/start-building-today-with-up-to-10-000-monthly-free-calls-per-product/) · [价格主页](https://mapsplatform.google.com/pricing/)

本项目共用到 **6 个** Google API（前端，统一用 `googleMapsApiKey`）：

| API | 在本项目中的用途 | 计费 SKU · 档位 | 每月免费额度 | 超额单价 | 官方文档 |
|---|---|---|---|---|---|
| **Maps JavaScript API** | 渲染每个行程详情页的地图、全局地图 `#/map`。地图实例**按行程缓存**（`tripMapCache`），每次"打开行程"约计 1 次 load | **Dynamic Maps** · Essentials | **10,000** loads | **$7.00 / 1,000** | [usage-and-billing](https://developers.google.com/maps/documentation/javascript/usage-and-billing) |
| **Routes API** | 驾车/步行/公交的**时间+距离+路线折线**：行车段药丸、点击连接线的路线视图（含逐步导航）、地图底图的交通段连线、插入式交通段。左侧药丸与地图线**共享同一次请求缓存**（`_dir`），不重复计费 | **Compute Routes** · Essentials（Basic） | **10,000** | **$5.00 / 1,000** | [usage-and-billing](https://developers.google.com/maps/documentation/routes/usage-and-billing) |
| **Places API（Text Search New）** | 地图搜索框加地点（`gplacesSearch`/`doMapSearch`）、按名解析 place ID 给详情卡、地理编码漏网的 Google 兜底（`recoverUnresolved`） | **Text Search (New)** · Pro | **5,000** | **$32.00 / 1,000**（Pro） | [places usage-and-billing](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing) |
| **Places UI Kit**（`<gmp-place-details>` 组件） | 点击标记/搜索结果时弹出的**富地点详情卡**（评分、营业时间、照片、评论、AI 摘要）。需在 Cloud 项目启用 "Places UI Kit"（`placewidgets.googleapis.com`） | **Places UI Kit** · Pro | **5,000** | **$5.00 / 1,000** | [places-ui-kit 概览](https://developers.google.com/maps/documentation/javascript/places-ui-kit/overview) · [产品页](https://mapsplatform.google.com/maps-products/places-ui-kit/) |
| **Maps Static API** | **PDF 导出**（📄 按钮）里打印的地图图片（`staticMapURL`：按类型的标记 + 和实时地图相同的交通线） | **Static Maps** · Essentials | **10,000** | **$2.00 / 1,000** | [static usage-and-billing](https://developers.google.com/maps/documentation/maps-static/usage-and-billing) |
| **Maps Embed API** | **仅作降级**：当 UI Kit 组件不可用/出错时，用免费的 Embed `iframe` 地点卡（`pdEmbedFallback`） | **Maps Embed** · Essentials | **无限免费** | **不计费** | [embed usage-and-billing](https://developers.google.com/maps/documentation/embed/usage-and-billing) |

注：
- Places 系列（Text Search / UI Kit / Place Details）的具体档位由请求的 **field mask** 决定，**账单取最高适用 SKU**；上表给出本项目典型用法对应的档位与单价。Maps Embed 官方原文：*"Maps Embed usage is available at no charge."*
- **对单人个人使用**：各 SKU 都远低于免费额度，实际基本 **$0/月**。
- 若地图出现 **"For development purposes only / This page can't load Google Maps correctly"** 水印，通常意味着 **Dynamic Maps 这个 SKU 触到额度上限或被限流**（而非计费问题）——去 Cloud Console 看该 key 的配额；同一 key 的 Places / Routes 数据接口往往仍可用。

## 🌐 其它外部服务（均 keyless，除 AeroDataBox 外）

| 服务 | 调用方 | 用途 | Key | 免费 / 使用政策 | 链接 |
|---|---|---|---|---|---|
| **Open-Meteo** | `server.py` | 天气：预报（`api.open-meteo.com`）+ ERA5 气候（`archive-api.open-meteo.com`） | 无 | 非商业免费、无需 key | [open-meteo.com](https://open-meteo.com/) |
| **Nominatim（OSM）** | `server.py` + `geocode.py` | 地理编码 search / reverse / city（`/api/geocode/*`） | 无 | 免费；**使用政策：≤1 req/s、必须带 User-Agent、不可重度批量** | [Nominatim 使用政策](https://operations.osmfoundation.org/policies/nominatim/) |
| **OSRM（demo）** | `index.html` | 无 Google key 时的**驾车路线兜底** | 无 | 公共演示服务器，**无 SLA**、仅供轻量/测试 | [project-osrm.org](https://project-osrm.org/) |
| **CARTO 瓦片（Voyager）+ Leaflet** | `index.html` | 无 Google key 时的**整张地图**（默认降级引擎；瓦片走 `basemaps.cartocdn.com`，底图数据 © OpenStreetMap、瓦片服务 © CARTO） | 无 | 免费档；遵守 CARTO 使用条款 | [leafletjs.com](https://leafletjs.com/) · [CARTO basemaps](https://carto.com/basemaps/) |
| **AeroDataBox（RapidAPI）** | `server.py`（`/api/flight`） | 航班**实时信息**（航站楼/登机口/机型/状态） | **要**（`aeroDataBoxKey`，**仅服务端，永不下发浏览器**） | 经 RapidAPI 的 freemium（有免费档，超出按量计费） | [RapidAPI 页面](https://rapidapi.com/aedbx-aedbx/api/aerodatabox) |
| **Wikipedia REST + Wikimedia Commons** | `covers.py` | 目的地**封面照片** | 无 | 公共 API，免费 | [Wikipedia REST](https://www.mediawiki.org/wiki/API:REST_API) · [Commons API](https://commons.wikimedia.org/w/api.php) |
| **OpenFlights `airports.dat`** | `airport_tz.py` | 构建 **IATA→时区**表（`data/airport_tz.json`，供航班跨时区时长计算） | 无 | 公共开放数据，**构建期一次性下载** | [jpatokal/openflights](https://github.com/jpatokal/openflights) |

> **只是外链、不发请求**：航班卡里的 FlightAware / FlightStats 追踪深链、地点卡的 "Google 地图 ↗" 深链——都是点击跳转的 URL，不算 API 调用。

## 🔑 Key 配置（`config.local.json`，已 gitignore）

| Key | 给谁用 | 是否下发浏览器 |
|---|---|---|
| `googleMapsApiKey` | 前端整套 Google（地图/地点/路线/UI Kit/Embed） | **会**（前端加载 Google JS 必需）。经 `GET /api/config` 下发，**用 HTTP referrer 限制到 `http://localhost:8787/*`**（手机访问再加相应地址），且从不作为静态文件暴露 |
| `aeroDataBoxKey` | 后端航班代理 | **永不下发**（`GET /api/config` 只回 `hasFlightKey` 布尔） |

**没有这两个 key 也能跑**（优雅降级）：地图退化为 **Leaflet + OpenStreetMap**，航班退化为**离线解析卡**（仍能解析航线/时刻/航司）。其余功能（天气、地理编码、封面）本就 keyless。

---

## 数据、隐私与分享

- **全本地、单用户**，默认仅回环监听，不联网同步；`HOST=0.0.0.0` 时也只放行回环 + Tailscale（`ALLOW_LAN=1` 额外放行私有网段）。
- `data/` 是 going-forward 真源；删除一趟行程**只动 `data/` 下的文件**。
- 对原始 `~/Documents/Travel Plan/` **只读 / 重命名（跟随标题）/ 追加拖入的文档**，**从不删除、从不覆盖**——每个写操作都被 realpath 限制在 `TRAVEL_DIR` 的直接子目录内。
- **行程隐私与 git**：仓库的 git 历史**不含任何个人数据**——`.gitignore` 排除了 `data/`（只保留两张生成的机场查询表）、`cache/`、`config.local.json` 与 `CLAUDE.md`；你的行程、封面、原文、策展表都只存在于本机磁盘（**注意：它们不在 git 里,也就没有 git 历史可回退**，请自行备份，如 Time Machine）。可以放心把仓库 push 到 GitHub；任何人 clone 后，自己的行程数据同样不会被误提交。

## 重新导入（常用命令）

```bash
python3 import/scan.py                       # 全量重扫（merge-safe，保留你的编辑/days）
python3 import/extract_pages.py --force       # 重抽所有 .pages 表
python3 import/geocode.py --all               # 给所有结构化但缺 places 的行程做地理编码
python3 import/covers.py --all                # 给所有缺封面的行程取目的地照片
python3 import/airport_tz.py                  # 重建 data/airport_tz.json + airport_geo.json
```
