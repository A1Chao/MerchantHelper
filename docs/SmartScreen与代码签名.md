# Windows 提示「Windows 已保护你的电脑」（SmartScreen）：为什么会弹、怎么让它不再出现

> 面向**使用者**的说明，写于 2026-10-17。
> 关于 Windows 行为的结论来自微软官方文档，关于本机的结论来自实测；
> 凡是推断而非实测的地方都单独标注了。

---

## 一、先看结论

| 你的文件是怎么拿到的 | 双击后看到什么 | 你该做什么 |
| --- | --- | --- |
| **QQ 群 / 微信 / U 盘**（本工具的主渠道） | 只有 UAC「你要允许此应用对你的设备进行更改吗？」 | 点「是」即可，本页可以不用看 |
| **浏览器下载**（GitHub Releases 网页 / 网盘网页） | UAC 之前先弹 SmartScreen「Windows 已保护你的电脑」 | 见 §三，做一次即可**永久**免除 |
| **Microsoft Store** | 只有 UAC | —— |

官方发行版**未做代码签名**，所以 UAC 里显示「未知发布者」，这是正常的。

---

## 二、SmartScreen 到底看什么

它只看两样东西，**都跟程序本身在做什么无关**：

### 1. 网络下载标记（Mark of the Web / MOTW）

浏览器下载的文件会被写上一个 NTFS 备用数据流 `Zone.Identifier`（内容形如 `ZoneId=3`）。
带这个标记 + 未签名 → 首次运行弹 SmartScreen。

微软官方 [SmartScreen reputation for Windows app developers](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)
（页面更新于 2026-05-06）里写死了几条：

> **EV certificates no longer bypass SmartScreen.** … **Paying a premium for EV solely to avoid
> SmartScreen warnings is no longer justified.**
>
> Valid Certificate (OV/EV)：**Warning** — app flagged as unrecognized until reputation accumulates.
>
> Self-signed Certificate：**Same behavior as no signature.**

→ 也就是：**买证书不能换来"首次下载不弹"**；EV 也没用；自签名等于没签。
（签名不是没用，它的价值是弹窗里显示「已验证的发布者」，但**首发仍会弹**。）

**U 盘拷贝、QQ/微信收文件、7-Zip 解压通常不会**带上这个标记。

### 2. NTFS 强制完整性标签（Mandatory Integrity Label）

**这一条容易被忽略**：即使文件**没有**上面那个下载标记，只要它的完整性标签是 `Low`，
双击时一样会先弹 SmartScreen。

本机实测（Windows 11）：**三份字节完全相同**的副本（同一个 sha256、同样大小、都没有下载标记），
只把完整性标签改成不同值，双击结果就不同：

| 完整性标签（`icacls` 读回） | 双击后的第一个弹窗 |
| --- | --- |
| `Mandatory Label\Low Mandatory Level` | **SmartScreen**（还没到 UAC 就被拦下） |
| `Mandatory Label\Medium Mandatory Level`／没有这一行 | 只弹 UAC |

查看方法（Windows 自带，不需要装任何东西）：

```powershell
icacls .\行商助手.exe | Select-String 'Mandatory Label'
# 出现 Low 才是问题；没有这一行、或显示 Medium，都是正常的
```

标签是**按目标目录继承**的：从压缩包解压、或在普通目录之间复制，副本通常是 `Medium`；
只有当**目标目录本身是 `Low`**（某些受限/沙箱化的下载目录）时，副本才会是 `Low`。

---

## 三、怎么让它不再出现

### 情况 A：浏览器下载的（带下载标记）

1. 右键那个文件（如果你下的是压缩包，就右键压缩包）→ **属性** → 最下面勾选
   **「解除锁定」** → 确定；
2. 再双击（压缩包先解压，然后双击里面的 `行商助手.exe`）。

做一次就够了，之后永远只弹 UAC。
**属性里没有「解除锁定」这一项，说明文件本来就没有被标记，直接双击即可。**

### 情况 B：不想动文件属性

用系统自带的 PowerShell 解压（实测不会把标记传给解压出来的文件）：

```powershell
Expand-Archive .\行商助手-v1.1.0.zip -DestinationPath 行商助手 -Force
```

### 情况 C：极少数——QQ/微信群拿到的文件也弹

那说明你接收到的副本被标成了 `Low` 完整性（少见）。在 PowerShell 里执行一次：

```powershell
icacls .\行商助手.exe /setintegritylevel Medium
```

再双击就只弹 UAC 了。（只改文件的元数据，不改内容，不影响文件哈希。）

### 关于「点『更多信息』→『仍要运行』」

也能让它跑起来，但**每次**都会再问一次，不算解决；用上面的办法才是永久免除。

---

## 四、从源码运行

从源码跑 `python main.py` 不经过资源管理器的启动检查，因此不会遇到 SmartScreen；
但**必须用管理员权限**运行（目标游戏以更高权限运行，普通权限下读不到按键）：
右键 → 以管理员身份运行，或在一个管理员 PowerShell 里运行。

---

## 五、参考

- 微软 · SmartScreen reputation for Windows app developers（本文主要依据）：
  https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation
- 微软 · Microsoft Defender SmartScreen 总览：
  https://learn.microsoft.com/en-us/windows/security/operating-system-security/virus-and-threat-protection/microsoft-defender-smartscreen/