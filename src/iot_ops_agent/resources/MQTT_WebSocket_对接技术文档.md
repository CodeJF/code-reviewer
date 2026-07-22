# 匿名 MQTT 与 WebSocket 对接说明

设备通过 `gateway` 完成 HTTP 登录和鉴权后连接 MQTT。`deviceShadow` 维护设备会话、订阅关系和在线状态，App 则通过 `/ws` WebSocket 接收状态变化。

## MQTT

排查 MQTT 连接失败时依次确认：

1. gateway 是否返回有效接入参数；
2. deviceShadow 是否收到 connect 请求；
3. 是否出现 unauthorized、timeout、disconnect 或重复登录；
4. 代理与 deviceShadow 的时间窗口是否一致。

## WebSocket

App WebSocket 由 `deviceShadow` 负责。握手失败时检查 `/ws` 路径、认证结果、upgrade 请求和 close code。正常的 close code 1000 不应单独判定为高风险；只有与 error、异常断开或业务失败证据同时出现时才升级。

如果连接正常但消息未送达，再检查 Redis 队列和 `pushService` 的推送结果。
