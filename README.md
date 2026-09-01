# 青岛栈桥海洋环境监测大屏

面向栈桥海水浴场的实时海洋与气象数据大屏，包含潮汐、天气、风况、浪高、气象预警和钉钉组合规则通知。大屏与通知配置页相互独立：

- 大屏：`/`
- 通知配置：`/notification-admin`

## 本地运行

需要 Python 3.10 或更高版本，无需安装额外 Python 依赖。

```bash
python OceanWindow_optimized.py --server
```

默认访问地址为 `http://127.0.0.1:5051/`。可通过环境变量 `PORT` 修改端口。

## 数据更新频率

| 模块 | 大屏更新频率 | 说明 |
| --- | --- | --- |
| 页面时钟 | 1 秒 | 页面不可见时暂停 |
| 潮汐源数据 | 1 小时 | 潮段状态和进度每分钟在本地重新计算 |
| 潮汐曲线 | 6 小时 | “现在”标线每分钟更新 |
| 天气与风况 | 10 分钟 | — |
| 浴场浪高和水温 | 1 小时 | — |
| 青岛近海浪高 | 15 分钟 | 与通知规则的数据缓存周期保持一致；接口异常时直接在大屏显示 |
| 气象与海洋预警 | 5 分钟 | 国家、山东和青岛预警合并显示在顶部预警条；山东预警每轮只请求一次 |
| 台风路径与云图 | 1 小时 | 使用北海预报减灾中心原版页面，显示数据源和最近刷新时间，也可通过模块按钮或大屏刷新按钮立即重载 |

各数据模块的标题区域会展示对应数据源和最近更新时间。浏览器标签页切到后台时，定时任务不会继续触发；返回页面后只补刷已经超过上述周期的模块，避免瞬间重复请求。点击“大屏刷新”会立即刷新所有数据模块、台风图和顶部预警条。

## 钉钉通知规则

通知引擎默认每 60 秒检查一次组合规则。通知侧天气缓存为 10 分钟、近海浪高缓存为 15 分钟，潮汐数据每日更新。同一规则在同一潮段只发送一次；潮段按潮汐数据的基准日期计算，跨午夜不会重复触发，并设有 5 分钟的同规则安全去重窗口。具体启用状态、通知时段、角色、Webhook、规则和发送记录均在 `/notification-admin` 中配置或查看。

生产环境应设置管理令牌：

```bash
NOTIFICATION_ADMIN_TOKEN=请替换为高强度随机字符串
```

通知配置和记录默认通过以下路径持久化：

```bash
OCEAN_NOTIFICATION_CONFIG=/data/notification_config.json
OCEAN_NOTIFICATION_DB=/data/ocean_notifications.db
```

## Docker 部署

```bash
docker build -t qingdao-ocean-dashboard:latest .
docker run -d --name ocean-dashboard \
  -p 7860:7860 \
  --env-file /etc/ocean-dashboard.env \
  -v /opt/qingdao-ocean-data:/data \
  --restart unless-stopped \
  qingdao-ocean-dashboard:latest
```

容器内服务端口为 `7860`。请确保宿主机的持久化目录可由容器内 UID `10001` 写入。
