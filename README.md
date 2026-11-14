# airlink-autopm

把一个 **伯虎空气质量检测仪** 的 PM2.5 读数和一台 **352 X83 空气净化器** 联动起来：
空气变差自动升档，空气转好自动降档，全程不碰电源开关。

因为两边都没有公开 API，这套东西的协议是从 352 生活 App 和小程序抓包 + 反编译逆向出来的。
它属于**个人自用的非官方集成**，厂商改协议就会失效。

```
伯虎检测仪 ──(HTTP, 云端)──┐
                          ├──> autopm.py ──(TLS + 自定义二进制协议)──> 352 X83
X83 自身传感器 ──(同一条 TCP)──┘
```

## 联动规则

```
eff = max(伯虎 PM2.5, 净化器 PM2.5)        # 双传感器取较大值

eff >  150                  -> 最大档（6 档）
eff >= 100                  -> 4 档
eff <  125  且当前是 6 档    -> 降到 4 档
eff <  80   且当前是 4 档    -> 降到 2 档
其他情况                     -> 保持不动
```

三条约束，都是为了不折腾机器：

* **升档即时，降档迟缓**：目标是"空气坏了立刻管"，而不是"空气好了立刻关小火"。
  上面每组升降阈值都留了 20~25 μg/m³ 的死区（迟滞带），读数在阈值附近抖动也不会来回换挡。
* **降档前必须在当前档位跑满 15 分钟**（`min_hold_s`），升档不受这条限制。
* **两屋隔离时自动放弃追随**（`give_up_s`，默认 30 分钟）：净化器只能净化自己所在房间。若本屋已干净（<80）而检测仪持续报高（≥100）达 30 分钟，说明门关着、两屋不通风，拉高档位毫无作用——此时暂停跟随检测仪，只按本屋读数调节；等检测仪回落（说明通风了）再恢复跟随。
  若本屋读数已低到个位数（`near_zero`，默认 20），观察时间缩短为 `fast_give_up_s`（默认 5 分钟）——这么干净的屋里开高档已经没有意义，没必要硬等 30 分钟；判定放弃的那一轮直接落到本屋读数对应的档位，不再受最小保持时间限制。
* **只看档位，不碰电源**：脚本永远不会开关净化器；你手动关机它就保持关着。

双传感器取 `max()` 的好处是一条式子同时满足两个诉求：任一传感器超标就升档，而两个都降下来才降档。

## 目录

| 文件 | 用途 |
|------|------|
| `autopm.py` | 主循环：取两边读数、判定档位、下发指令、写日志 |
| `c352.py` | 352 云端协议实现（登录签名、TCP 控制通道、帧编解码） |
| `login.py` | 一次性短信登录，把 token 写进 `config.json` |
| `discover_device.py` | 一次性取出净化器的 `mac` / `authCode` / `companyCode` / `deviceType` |
| `install_task.ps1` | 注册 Windows 计划任务（含看门狗设置） |
| `config.example.json` | 配置模板 |

## 部署

需要 Python 3.9+，一台长期开机的 Windows 机器。

```powershell
pip install -r requirements.txt
copy config.example.json config.json
```

**1. 拿伯虎的数据参数**

伯虎检测仪的实时数据走一个很朴素的小程序接口，登录微信开发者工具或抓一次包就能看到：

```
GET http://www.bohu-tech.com/iot/api/latest?device_id=<你的设备ID>
Cookie: uid=<你的用户ID>
```

把 `bohu_uid`、`bohu_device_id` 填进 `config.json`。注意要用 `http://`（`https` 会被 WAF 拒连）。

**2. 登录 352 账号**

```powershell
python login.py 13800000000
```

会发短信验证码，输入后 `config.json` 里就有 `access_token` / `refresh_token` / `air_token` 了。

**3. 取净化器参数**

控制帧里需要一个 `authCode`，厂商只把它放在老接口里，而那个接口用 App 内嵌的 RSA 私钥签名。
用你自己安装的 App 副本（APK）把私钥抽出来：

```powershell
python -c "import re,zipfile;z=zipfile.ZipFile('352life.apk');d=b''.join(z.read(n) for n in z.namelist() if n.endswith('.dex'));open('privkey.hex','w').write(re.search(rb'308204BD[0-9A-F]{2400}',d).group().decode())"
python discover_device.py
```

`privkey.hex` 已在 `.gitignore` 里，不要提交它——它是厂商的凭据，不属于这个仓库。

**4. 跑起来 / 装成常驻任务**

```powershell
python autopm.py                                   # 前台试跑
powershell -ExecutionPolicy Bypass -File install_task.ps1   # 后台常驻
```

日志在本目录 `autopm.log`。

```powershell
Get-Content autopm.log -Tail 20                    # 看状态
Stop-ScheduledTask airlink-autopm                  # 停
Start-ScheduledTask airlink-autopm                 # 起
```

## 踩过的坑（如果自己改代码，这几个值得知道）

1. **计划任务默认 72 小时执行上限**。长时间运行的任务满 72 小时会被 Windows 静默杀掉（返回码
   `0x41306`），而且只配登录触发器的话再也不会自己起来。`install_task.ps1` 里把它设成无限，
   并加了"每 5 分钟重复 + 单实例"当作看门狗——挂了 5 分钟内自动复活。
2. **`air_token` 的有效期远短于登录返回的 `expires_at`**（那是 `access_token` 的 60 天）。
   它过期后 JOIN 会被服务器拒绝（应答字节 `82 01`）。脚本每次启动和 JOIN 被拒时都会刷新一次。
3. **`refresh_token` 是一次性的**，每用一次就轮换，所以刷新后必须立刻落盘；
   刷新失败不能阻塞控制——只要手上的 `air_token` 还活着，继续用。
4. **净化器断电/换网后会停止推送状态帧**。如果只依赖被动推送，脚本会拿着几分钟前的旧读数做判断
   （曾经因此漏报一次真实污染）。所以这里每分钟主动查一次状态，超过 3 分钟没新数据就重连。
5. **TCP 层的锁不能嵌套**。心跳线程持锁后又去取帧序号（同一把非重入锁）会永久死锁，
   表现是"查询正常但任何调档指令都不生效"。所有帧都先构造完再进锁。
6. **伯虎上报周期约 6 分钟**，几十秒的瞬时尖峰在云端根本看不到——这也是加上净化器自身传感器做双判据的原因。

## 协议概要

仅供理解代码，不是官方文档。

**HTTP（`app.352air.com`）**：`Authorization: Token <access_token>`，
`ts` 为秒级时间戳，`sign = md5(appid + path + ts)`。

**TCP（`wifi.352air.com:11591`，TLS，客户端不校验证书）**：

```
0x81                    取一台工作服务器（应答里带 IP + 端口）
0x82        JOIN        携带 JOIN_CODE(6B) + 0xFF + air_token，应答 0 表示成功
0x89        异常登录     加入后必须先发，否则设备不理会控制帧
0x86 01 02  订阅设备事件
0x83        查询在线
0x61        心跳，5 秒一次
```

控制帧（发给设备的命令）外层是 16 字节头：`A1 | 04 | MAC(6) | len+7 | 00 | seq(2) | companyCode | deviceType | authCode(2)`，
再拼 `0x01` 和应用层字节：

| 动作 | 字节（末尾是校验和） |
|------|--------------------|
| 风量 | `A5 A0 52 <gear 1-6> 00 <sum>` |
| 开关 | `A5 A0 5E <35 开 / 11 关> 00 <sum>` |
| 模式 | `A5 A0 51 <mode> 00 <sum>` |
| 查状态 | `A5 A0 11 11 00 00` |

状态帧 `cmd=0x02`：`payload[4]`=档位，`payload[9]`=开关（0 开 / 0x11 关），`payload[12:14]`=PM2.5（大端）。

## 说明

* 只做了 352 X83 和一个型号的伯虎检测仪，其他型号大概率要调协议细节。
* 厂商随时可能改接口；坏了自己看 `autopm.log` 再抓一次包。
* 仓库里没有任何真实凭据：`config.json`、`privkey.hex`、日志都在 `.gitignore` 中，
  模板 `config.example.json` 只有空字段。
* 仅限个人家庭自用。请遵守相关服务条款。

## License

MIT
