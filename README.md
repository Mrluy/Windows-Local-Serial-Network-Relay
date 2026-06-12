# Windows 本地串口网络中继

这是一个本地串口到 TCP/UDP 网络的透明透传工具。运行在连接串口设备的 Windows 电脑上，其它设备连接或被本机连接后，就可以通过网络与本机 COM 串口双向收发原始数据。

## GUI 单文件 EXE

已经提供 GUI 入口：

```powershell
python .\serial_tcp_relay_gui.py
```

GUI 支持：

- 选择串口、波特率、数据位、校验位、停止位、DTR/RTS
- 绑定“允许所有”、回环地址或检测到的本机 IPv4 地址
- 网络模式支持 `TCP Server`、`TCP Client`、`UDP Server`、`UDP Client`
- 设置本地绑定地址、本地端口、目标地址和目标端口
- Server 模式支持单对端或多对端策略
- 黑名单 / 白名单访问控制，支持 IP、CIDR 和通配符，例如 `192.168.1.20`、`192.168.1.0/24`、`192.168.1.*`
- 所有网络模式均为双向透明透传
- 当前连接、连接记录、运行日志、十六进制数据日志、日志导出
- 自动保存上次配置

打包单文件 EXE：

```powershell
.\build_exe.ps1
```

生成文件：

```text
dist\SerialTcpRelay.exe
```

这个 EXE 已包含 Python 运行时和串口依赖，目标电脑无需安装 Python 或其它依赖。首次监听局域网端口时，Windows 可能弹出防火墙授权提示，请允许专用网络访问。

软件窗口和 EXE 文件图标来自 `img\app.png`，打包脚本会自动转换成 Windows 需要的 `.ico` 格式并嵌入。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 查看串口

```powershell
python .\serial_tcp_relay.py --list-ports
```

## 启动透传

示例：把本机 `COM3` 以 `9600 8N1` 暴露到局域网 TCP `10123` 端口。

```powershell
python .\serial_tcp_relay.py --com COM3 --baudrate 9600 --listen 0.0.0.0 --port 10123
```

其它设备连接：

```text
本机IP:10123
```

例如本机 IP 是 `192.168.1.20`，其它设备就连接 `192.168.1.20:10123`。

## 常用参数

```text
--com COM3              串口号
--baudrate 9600         波特率
--data-bits 8           数据位，支持 5/6/7/8
--parity N              校验位，支持 N/E/O/M/S
--stop-bits 1           停止位，支持 1/1.5/2
--listen 0.0.0.0        监听地址，0.0.0.0 表示允许局域网访问
--port 10123            TCP 监听端口
--multi-client          允许多个 TCP 客户端同时连接
--hex-log               打印收发数据的十六进制日志
--no-dtr --no-rts       关闭 DTR/RTS 控制线
```

默认只允许一个 TCP 客户端连接，避免多个上位机同时操作同一个串口导致协议冲突。确实需要多设备旁路监听或广播时，再加 `--multi-client`。

## Windows 防火墙

如果其它设备无法连接，请检查 Windows 防火墙是否允许当前 Python 程序或 TCP 端口入站访问。也可以先临时改用一个明确端口，例如 `10123`，再在防火墙里放行该端口。

如果启动时提示端口已被占用，请关闭占用该端口的程序，或在软件中改用其它本地端口。

## 注意

本工具是原始字节透传，不会把 Modbus RTU 转成 Modbus TCP，也不会修改任何协议帧。TCP 收到什么就写入串口，串口收到什么就发回已连接的 TCP 客户端。
