# HD2 大厅搜索器（HD2LobbyBrowser）

在游戏外列出《绝地潜兵2》Steam 公开大厅的小工具，支持按**任务 / 星球 / 战线 / 地区 / 平台 / 延迟 / SOS** 筛选，
点一下就能进房。**不注入游戏、不读游戏内存、不碰 EAC**。

> 当前版本：**V21**
> 作者：QLongYan ｜ 视频与更新：B站 [space.bilibili.com/38739761](https://space.bilibili.com/38739761)
> 备用发布页：A网 [ayakamods.com/mods/hd2lobbybrowser.6133](https://ayakamods.com/mods/hd2lobbybrowser.6133/)

---

## 一、下载与使用

**普通用户不用看这个仓库的源码，直接下载即可：**

1. 打开右侧 **Releases**（或点 [最新版](https://github.com/QLongYan/HD2LobbyBrowser/releases/latest)）
2. 下载 `HD2LobbyBrowser.exe`
3. 双击运行。需要 **Steam 客户端已启动且已登录**，不需要启动游戏（进房时游戏要开着）

单文件绿色版，约 13 MB，自带 Python 运行时，**免安装、免前置**。
第一次运行若被 Windows Defender / SmartScreen 拦，选「更多信息 → 仍要运行」即可（无签名程序的常见提示）。

## 二、功能

| 功能 | 说明 |
|---|---|
| 房间列表 | 通过 Steam 公共大厅接口枚举，一次扫 6–9 个活跃星球，通常 200–350 个房 |
| 任务筛选 | 任务名来自游戏发布的 mission_hash，可在面板里多选、搜索、全选 |
| 星球筛选 | 战线 → 星系 → 星球 三级联动，带区内玩家数 |
| 地区 / 延迟 | 亚洲 / 北美 / 欧洲 / 大洋洲 / 南美 / 中东 / 非洲，显示预计延迟 |
| 平台 | 房主是 PC / PS / Xbox（来源：PlayFab 公开资料字段） |
| SOS | 只留 SOS 房 |
| 房主名 | 跨平台房也能显示 |
| 排序 | 星球 / 战线 / 难度 / 人数 / 延迟 / 地区 / 房主 / 平台 / SOS / 建立时间 |
| 保存预设 | 记住全部筛选状态，下次启动自动套用并直接开搜 |
| 自动更新 | 每次启动检查 GitHub Release，有新版本会提示并点亮「升级」 |
| 全局热键 | `Ctrl+Alt+H` 唤起 / 收起窗口 |

## 三、数据来源（为什么它能显示任务名和平台）

| 信息 | 来源 |
|---|---|
| 房间、星球、人数、SOS、隐私 | Steam `ISteamMatchmaking` 大厅字段 |
| 难度 | 大厅键 17；任务数据包内也带一份（优先用包内的） |
| 任务名 | 大厅键 10 的 `f3fc…` 数据包，取 53–60 位 `mission_hash`，查内置 126 条对照表 |
| 平台 / 房主名 | PlayFab 公开资料 `testament_backing_platform`（0=PS / 1=Steam / 4=Xbox） |

全程只发 HTTPS 只读请求，**不接触游戏进程**。

## 四、从源码运行

```bat
pip install pywebview
python 源码\hd2lobby_app.py
```

需要 Windows 10/11 + Python 3.9+，且本机装有 **WebView2 Runtime**（Win11 自带，Win10 多数已有）。

## 五、自己打包 exe

双击 `build.bat`，或在仓库根目录执行：

```bat
pip install pyinstaller pywebview
pyinstaller --noconfirm --onefile --noconsole --name HD2LobbyBrowser ^
  --add-data "源码\planets.json;." ^
  --add-data "源码\planets_zh.json;." ^
  --add-data "源码\sectors_zh.json;." ^
  --add-data "源码\mission_names.json;." ^
  --add-data "源码\factions.json;." ^
  --add-data "源码\sdr_pops.json;." ^
  --collect-all webview --collect-all clr_loader --collect-all pythonnet ^
  --hidden-import webview.platforms.edgechromium 源码\hd2lobby_app.py
```

产物在 `dist\HD2LobbyBrowser.exe`。

## 六、自动更新怎么工作

程序启动后会在后台请求：

```
https://api.github.com/repos/QLongYan/HD2LobbyBrowser/releases/latest
```

- 拿到 `tag_name`（形如 `v22`）与 `1` 比较：**大于当前版本** → 标题旁显示金色「有新版 V22」，「升级」按钮点亮
- 等于 → 显示「已是最新版」，按钮置灰
- 请求失败（断网 / API 限流）→ 显示「检查更新失败」，按钮仍可手动点

点「升级」时，程序按顺序尝试：

1. 仓库根目录 `update_url.txt` 里配置的自定义直链（可选，默认留空）
2. Release 资产里第一个 `.exe`
3. `https://github.com/QLongYan/HD2LobbyBrowser/releases/latest/download/HD2LobbyBrowser.exe`
4. 镜像：`ghproxy.net` / `gh-proxy.com` / `ghfast.top`

下载后校验 `MZ` 头与体积（>5MB），通过才替换：旧 exe 改名 `.old` → 写入新版 → 启动新进程 → 退出。
任何一步失败都不会改动现有文件，并弹出提示。

## 七、目录

```
README.md                 本文件
LICENSE                   MIT
CHANGELOG.md              更新日志
build.bat                 一键打包脚本
发布指南.md                怎么在 GitHub 上发新版本（给自己看的）
release-notes-v21.md      V21 的 Release 说明（复制粘贴用）
源码\
  hd2_lobby_browser.py    核心：Steam 大厅、PlayFab、HTTP 服务、页面
  hd2lobby_app.py         窗口入口（pywebview + 全局热键）
  *.json                  星球 / 星区 / 任务名 / 阵营 数据
```

## 八、免责声明

- 本工具通过 Steam 公开接口与 PlayFab 公开资料读取数据，**不修改游戏文件、不注入进程**。
- 使用第三方工具存在账号风险，请自行判断；作者不对任何封号、数据异常负责。
- 与 Arrowhead Game Studios、Sony、Valve 均无关联。
- 请勿将本工具用于商业用途。

## License

MIT，详见 [LICENSE](LICENSE)。
