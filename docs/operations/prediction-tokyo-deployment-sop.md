# Prediction 东京新主机部署 SOP

本 SOP 面向腾讯云 Lighthouse 东京或另一台全新 Linux 主机，部署独立的
Prediction，并通过本地 Mac Gateway 和 SSH 隧道访问。默认目标是无凭据、
N-leg 暂停的 Shadow；它不接管原生产实例，不提交订单。购买、配置、部署、
凭据迁移和交易各自需要相应授权；本文件不是执行这些操作的授权。

先读 [云端拓扑及门禁](prediction-cloud.md)、
[Deployment Preflight](deployment-preflight.md)、
[验证及交付规则](agent-verification.md) 和
[依赖规则](dependency-reproducibility.md)。使用所选发布自己的脚本和规则。
下列命令由获得授权的操作员执行；占位符必须替换，不能直接照抄为实际证据。

## 1. 已验证范围与未完成事项

2026-10-09 的历史部署选择了 GitHub main 历史中的发布
`2797cd5029c7812e9405825674f559e7d1ebafaa`，使用
[main-push CI 37930163820](https://github.com/raymizzou/open_trader/actions/runs/37930163820)。
全新 Ubuntu 24.04.4 x86_64 主机上的无凭据 Shadow 完成了两主机 Host
Readiness `READY`、正式 wrapper install/start 和 Cloud Smoke `HEALTHY`。
这些结果只覆盖当时的 SHA、配置和运行状态，不是新会话的当前健康证明。

| 配置阶段 | 已知事实 | 仍需验证 |
| --- | --- | --- |
| 无凭据 paused Shadow | backend=disabled，无 LP account reader；LP 503 是预期；N-leg paused，candidate_exclusions=false | 不能据此认证 LP、完整池遍历或交易容量 |
| 凭据读取 | Main 随后私下导入既有凭据；带 require-trading-region 的 read-auth 初始化 ready，完整认证账户读取成功，mutation/notification attempts 均为 0 | 总结果因 trading_region blocked 而 BLOCKED；认证读取成功不等于交易可用 |
| 认证 paused Shadow | 同一接受 SHA 曾 RUNNING，认证读取成功；当时真实 Dashboard authenticated=true、stale=false，完整 orders/positions/trades 与 balance 读取，N-leg paused，guard attempts 为 0；本次尝试现已被资源 guard 停止 | 认证 Cloud Smoke FAILED，输出 ROLLBACK 标签但未执行回滚；metadata transport 错误与服务内存预算触发分别未解决；完整 feed 状态 UNKNOWN，完整 catalog/candidate/history 遍历、24 小时容量和 12 小时刷新尚未验收 |
| 实盘/订单诊断 | 未提交订单；现有 Shadow 禁写和 production 地域门禁不变 | 新诊断执行路径/例外需要独立具体方案批准 |

认证 Smoke 的远端直接诊断为 `runtime logs missing or contain errors`。
启动 journal 反复出现
`lp_metadata_read_failed stage=market|event error_types=TransportError>RemoteProtocolError>RemoteProtocolError`。
该阶段认证/账户 Dashboard 可用；上游 transport 错误原因尚未确认。不能沿用此前
无凭据 profile 的历史 `HEALTHY` 宣称认证 full feed 健康。

无凭据空闲样本约 130 MiB cgroup current、133 MiB peak，180 个采样中
121 个运行样本，memory events 为 0、swap used 为 0。它不是认证 LP
负载容量证据，也不能代替完整启动和长期观测。

## 2. 购买与主机核查

东京 Linux 2 vCPU / 4 GB 可作为初始容量规划建议，不是全 LP 负载认证。
选择 Ubuntu 24.04 LTS，核对套餐自带出网流量及超额计费；基础实验无需
无关附加产品。不要把已有 CentOS 8 示例的包管理或 Python 路径套到新主机。

本地生成或选择专用 Ed25519 密钥，仅将公钥导入**同一区域**并在线绑定
`ubuntu` 用户。私钥留在操作员本机。先备份要改的 SSH 配置和 known_hosts；
使用任务专属 alias，例如 `open-trader-tokyo-shadow`：

```sshconfig
Host open-trader-tokyo-shadow
    HostName <operator-private-host-address>
    User ubuntu
    IdentityFile <operator-local-private-key-path>
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    ForwardAgent no
    ControlMaster no
    ControlPath none
```

通过可信控制台渠道核对主机指纹，再登记 known_hosts；`ssh-keyscan` 的输出
本身不是身份认证。不要启用 root SSH、关闭 host-key 校验或转发 agent。
验证所选 alias、非交互登录和 `sudo -n` 后，再执行远端准备。仅需管理 SSH
入口；Prediction 8769 和本地 Gateway 都保持 loopback，不开放公网业务端口。

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no \
  open-trader-tokyo-shadow 'id; cat /etc/os-release; uname -m; python3.12 --version; free -h; swapon --show; df -h /; sudo -n true; sudo -n ss -ltnp'
```

记录实际 OS、登录用户、架构、Python、磁盘、MemAvailable、swap、已有服务和
端口。无法核对或发现冲突时停止；先识别现有服务，不能杀掉无关进程来腾端口。

## 3. 一次性初始准备：源码、工具与运行环境

本节在新主机上通过已验证的 `ubuntu` 登录执行。进入授权的 `sudo -n bash`
准备会话，而不是开放 root 登录。以下 shell 块均假定 Bash。
选择完整 40 位 SHA，并先核对它仍在 GitHub main 历史、对应 main-push CI
成功且 artifacts 未过期。Artifacts 保留 3 天；PR head、PR merge SHA、测试镜像
不能替代 main-push 证据。实际正式 wrapper 会再次读取 GitHub核验。

```sh
set -euo pipefail
umask 022
SHA='<accepted-40-hex-main-sha>'
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]]
RELEASE="/opt/open-trader/releases/$SHA"
VENV="/opt/open-trader/venvs/$SHA"
RUNTIME=/var/lib/open-trader/prediction
UV=/opt/open-trader/tools/uv-0.12.19/bin/uv
export PYTHONDONTWRITEBYTECODE=1
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3.12-venv ca-certificates git curl
install -d -o root -g root -m 0755 /opt/open-trader \
  /opt/open-trader/releases /opt/open-trader/venvs /opt/open-trader/tools \
  /var/lib/open-trader /etc/open-trader
if ! getent passwd prediction >/dev/null; then
  useradd --system --user-group --home-dir "$RUNTIME" \
    --shell /usr/sbin/nologin prediction
fi
test "$(id -gn prediction)" = prediction
install -d -o prediction -g prediction -m 0700 "$RUNTIME" "$RUNTIME/config"
test ! -e "$RELEASE" && test ! -L "$RELEASE"
test ! -e "$VENV" && test ! -L "$VENV"
git clone --no-checkout https://github.com/raymizzou/open_trader.git "$RELEASE"
git -C "$RELEASE" checkout --detach "$SHA"
test "$(git -C "$RELEASE" rev-parse HEAD)" = "$SHA"
test "$(git -C "$RELEASE" rev-parse --abbrev-ref HEAD)" = HEAD
test ! -e /opt/open-trader/tools/uv-0.12.19
python3.12 -m venv /opt/open-trader/tools/uv-0.12.19
/opt/open-trader/tools/uv-0.12.19/bin/python -m pip install \
  --disable-pip-version-check uv==0.12.19
"$UV" --version
cd "$RELEASE"
export UV_PROJECT_ENVIRONMENT="$VENV"
"$UV" lock --check --python /usr/bin/python3.12 --no-python-downloads
"$UV" sync --locked --python /usr/bin/python3.12 --no-python-downloads \
  --only-group build --no-build
"$UV" sync --locked --python /usr/bin/python3.12 --no-python-downloads \
  --no-default-groups --group build --no-install-project --no-build-isolation
"$UV" sync --locked --python /usr/bin/python3.12 --no-python-downloads --offline \
  --no-default-groups --group build --no-build-isolation
"$UV" pip check --python "$VENV/bin/python"
```

此最小 runtime 不装 dev、cloud-ssm、browser extras。仅 SSM profile 才添加
锁定的 cloud-ssm；客户端浏览器依赖在客户端独立准备。不要复制 macOS venv。
本次主机系统 Python 3.12.3 被生产 preflight 接受；开发基线 3.12.14 是不同
要求。未来仍需所选发布锁与实际 Python 3.12 环境检查，不能泛化为任意版本支持。
源码必须 root-owned、detached、clean，无 release symlink；所有祖先及 venv
路径不得 group/world writable。Python symlink 目标也须为可信 root-owned 路径。
运行期写入只进独立 service-owned runtime。`Dockerfile.dev` 是测试镜像。

### 本次遇到的两个环境阻断

uv 创建的 venv `.lock` 曾为 0666，触发可信路径拒绝。只修正已确认属于本任务
venv 的这一个普通 root-owned 文件；不要递归 chmod 整棵树掩盖其他漂移：

```sh
test -f "$VENV/.lock" && test ! -L "$VENV/.lock"
test "$(stat -c %u "$VENV/.lock")" = 0
chmod 0600 "$VENV/.lock"
stat -c '%U:%G %a %n' "$VENV/.lock"
namei -l "$RELEASE" "$VENV/bin/python" "$RUNTIME/config"
```

最小 runtime 还缺少 Deployment Preflight 导入的 `packaging`。从**所选发布**
`uv.lock` 的 packaging version 和 wheel hash 生成一次性 requirements；不要
解析 latest，也不要改 lock。下面生成文件，不执行下载的代码：

```sh
REQ=$(mktemp /var/tmp/open-trader-preflight-req.XXXXXX)
/usr/bin/python3.12 - "$RELEASE/uv.lock" "$REQ" <<'PY'
import pathlib, sys, tomllib
lock = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
packages = [p for p in lock['package'] if p['name'] == 'packaging']
assert len(packages) == 1
p = packages[0]
wheels = [w for w in p['wheels'] if w['url'].endswith('-py3-none-any.whl')]
assert len(wheels) == 1
pathlib.Path(sys.argv[2]).write_text(
    f"packaging=={p['version']} --hash={wheels[0]['hash']}\n")
PY
"$UV" pip install --python "$VENV/bin/python" --require-hashes --no-deps -r "$REQ"
rm "$REQ"
chmod 0600 "$VENV/.lock"
"$VENV/bin/python" -B -c 'import packaging; print(packaging.__version__)'
test -z "$(git -C "$RELEASE" status --porcelain)"
```

本次 lock 为 packaging 26.2，wheel SHA256 为
`5fc45236b9446107ff2415ce77c807cee2862cb6fac22b8a73826d0693b0980e`。
未来以所选 lock 为准。每次安装后复查 `.lock` 和路径权限。保留源码清洁；
源读取命令用 `-B`/`PYTHONDONTWRITEBYTECODE=1`，不要向 src/scripts 写 bytecode。

## 4. 配置及凭据阶段

准备经过操作员检查的**非秘密** `prediction_arbitrage.json`；
`polymarket_trading.load_trading_config` 只接受 `signer_address`、`wallet_address`
及可选 `predict`（其字段为 `wallet_address`、`environment: mainnet`）。
Auto 状态另行持久化，N-leg 暂停由云配置/unit 控制，不能写入这个账户配置。
Shadow 不复制本地交易/runtime 数据库，也不能把未知账户编造成空账户。
在 root 准备会话中用 `install -o prediction -g prediction -m 0600`
放入 `$RUNTIME/config/prediction_arbitrage.json`。runtime/config 都为 0700。
本 SOP 不提供一个假设适用于所有账户的运行配置。

生成 root-owned 0600 的正式非秘密云配置：

```sh
cat > /etc/open-trader/prediction-cloud.json <<JSON
{
  "release_root": "$RELEASE",
  "runtime_root": "$RUNTIME",
  "python": "$VENV/bin/python",
  "user": "prediction",
  "expected_sha": "$SHA",
  "mode": "shadow",
  "n_leg_paused": 1,
  "candidate_exclusions": false
}
JSON
chown root:root /etc/open-trader/prediction-cloud.json
chmod 0600 /etc/open-trader/prediction-cloud.json
export OPEN_TRADER_PYTHON="$VENV/bin/python"
bash scripts/prediction-systemd.sh render
bash scripts/prediction-systemd.sh preflight
```

`PRECHECK_OK` 是远端组件结果，**不是**两主机 `READY`。无凭据 profile 不读
Keychain/SSM/账户，固定 backend=disabled，LP 503 属于该 profile 的合同。

认证 paused Shadow 另需 `credential_backend: file` 和
`credentials_file: /var/lib/open-trader/prediction-credentials/polymarket.json`。
目录 prediction-owned 0700、普通文件 prediction-owned 0600，放在 release 和
runtime 之外；拒绝 symlink、错误 owner/mode/字段，无失败回退。
仅把路径写入 unit。操作员私下读取既有源 key 和账户引用，核对匹配后以
内存流经 SSH stdin 传给远端受控接收程序。接收端用私有 umask、核对目录和
文件身份/权限；内容不得进 stdout、日志、Git、环境变量或 argv。不要打开
shell tracing、打印 Keychain 导出或在会话中粘贴秘密。本次 Main 已完成这类
私下导入；本文件不要求重复导入，也不授权创建/替换/撤销 API key。

使用 service 身份和与 unit 一致的非秘密 backend/path 环境执行
`prediction-arb wallet read-auth --config <runtime-config>`，确认 GET-only
认证派生与完整 orders/positions/trades/balance/allowance 读取分别成功；记录
mutation/notification attempts。`data-check --config <runtime-config> --sample 5`
是另一项数据检查，最佳 `PARTIAL` 也不等于完整池/live-feed 验收。普通 SDK
`create()` 可能创建凭据或钱包，不能替代只读 read-auth。

生产仍按现有 SSM-only profile，使用固定 secret version、最小 CAM 权限、
明确 SSM region、metadata isolation 和 old-owner 停止证据。file profile
不成为生产通道。具体非秘密字段和切换条件见 [凭据与所有权](prediction-cloud.md)。

## 5. 地域、公开可达与订单测试

本次东京地址得到 geoblock `blocked=true`、country JP，同时公共 CLOB time
和 Gamma markets GET 均为 HTTP 200。公共可达不能证明认证交易可用。
任务证据指出官方 [API 地域说明](https://docs.polymarket.com/api-reference/geoblock)
称日本 API 不受限，而 [帮助页](https://help.polymarket.com/en/articles/13364163-geographic-restrictions)
的地域说明不同；此文档未重新联网核对，保留该冲突为未知原因，不能用文档
解释覆盖实际 gate。不得绕过限制、使用 proxy/VPN 规避或伪造 region attestation。
production 的 require-trading-region 当前阻断；Shadow 禁止所有交易 POST。

### 人工单笔诊断 CLI（#321）

新增独立入口 `python -m open_trader.polymarket_order_probe` 和薄脚本
`scripts/polymarket-order-probe.sh`。本入口不修改 Shadow、production 地域门禁或
Auto。用户手工指定 token、价格和数量；工具不扫描或推荐市场。
**实盘下单尚未验证；以下交易命令只供用户手动执行。** GET 成功仅说明认证及
读取成功，不能证明交易可用。地域报告保留真实 `blocked`、country 和 region，
隐藏 IP；结果未知时明确显示 unknown，不伪造地域、不提供代理或替代 HTTP 地址。

`check` 只读已有认证、账户余额/allowance、挂单、成交、持仓、市场、盘口、tick、
最小数量和 CLOB 时间，不创建 API key 或钱包。它验证输入但保持订单验证状态
`UNVERIFIED`。`run` 仅允许 **BUY / post-only / GTD**，名义金额硬上限
**1 USDC**，没有提高上限的参数。价格或数量无效、报价跨 ask、事实缺失/过期、
市场不接受订单、余额/allowance 不足、tick 或最小数量不满足时均不提交。
签名中的份额和金额须精确对应用户输入，不截断数量或将有损取整视为匹配。
无法精确编码为交易所金额或份额单位的输入在提交前拒绝，包括长精度的小数尾数。
CLOB 时间 +240 秒用于到期；SDK 要求至少 180 秒，提交前再次读取时间，准备
耗时过长时阻断，不退回 GTC。提交前还按最终 CLOB 时间核对盘口年龄不超过
10 秒；该检查独立于总本地事实年龄 10 秒和 GTD 至少 180 秒的检查。名义上限
不表示本工具保证无成交或无资金风险。

运行前，用户须停止同钱包其他交易写入，或改用独立测试账户。工具不自动停止
production。`--confirm-single-writer` 记录**人工确认**，不是跨主机技术锁证明。
`--confirm-live` 明确允许该命令的一次 POST，以及已知订单 ID 的撤单 DELETE。
地域信息单独报告；若 CLOB 拒绝，结果保持 `REJECTED`，不能因其他 GET 成功而
改称交易成功。只有提交阶段的拒绝能分类为订单 `REJECTED`；已接受订单的后续
GET 401/403/404 等读取失败保留明确订单 ID、已观察 LIVE 和撤单 ACK，结果为
`UNKNOWN`，不能把 GET 状态码记作 POST 拒绝。SSM backend 使用已有
region/secret/version/role 引用和隔离前提，
不得为诊断添加凭据、降低权限或改变正式 backend。

显式选择锁定 Python、账户配置和 backend。以下 file 示例只传凭据路径，不传
秘密内容；凭据目录须属当前用户且为 0700，凭据文件为 0600，并遵循现有 file
backend 结构。不要将密钥、签名、secret 或 bearer header 放入命令参数或日志。
将 `<...>` 替换为已验证路径及人工选择的值后再执行：

```bash
export OPEN_TRADER_PYTHON='<absolute-lock-matched-python3.12>'
PROBE='<absolute-release-root>/scripts/polymarket-order-probe.sh'
ACCOUNT='<absolute-account-config.json>'
CREDENTIALS='<absolute-private-credential-file.json>'
TOKEN='<manually-selected-token-id>'
PRICE='<explicit-buy-price>'
QUANTITY='<explicit-share-quantity>'

# GET-only；不表示订单可提交或已提交。
"$PROBE" check --config "$ACCOUNT" --credential-backend file \
  --credentials-file "$CREDENTIALS" --token "$TOKEN" --price "$PRICE" --quantity "$QUANTITY"

# 用户确认写入所有权后，创建专属私有回执目录。每次诊断选一个新文件名。
umask 077
mkdir -m 700 '<absolute-private-probe-directory>'
RECORD='<absolute-private-probe-directory>/attempt-001.json'

# 有真实 POST/DELETE 副作用；仅由用户手动执行。
"$PROBE" run --config "$ACCOUNT" --credential-backend file \
  --credentials-file "$CREDENTIALS" --token "$TOKEN" --price "$PRICE" --quantity "$QUANTITY" \
  --record "$RECORD" --confirm-live --confirm-single-writer

# GET-only 读取回执中明确的订单；不猜测订单 ID。
"$PROBE" status --config "$ACCOUNT" --credential-backend file \
  --credentials-file "$CREDENTIALS" --record "$RECORD"

# 恢复撤单：用户再次手动确认；只针对该回执中的明确订单 ID。
"$PROBE" cancel --config "$ACCOUNT" --credential-backend file \
  --credentials-file "$CREDENTIALS" --record "$RECORD" --confirm-live --confirm-single-writer
```

模块入口接受完全相同的参数。keychain 或 tencent-ssm 必须显式选择相应 backend；
只有 file backend 使用 `--credentials-file`。运行回执要求绝对路径、私有 0700
父目录和 0600 普通文件，拒绝符号链接、错误权限及账户/backend 身份冲突。工具
原子更新并 fsync 回执，POST 前保存 `attempted=true`。同一路径的本地排他锁
只防止该回执并发使用，不证明钱包全局互斥；保留回执和 `.lock` 文件。
已有回执不允许 `run` 重发；不要删除回执或换文件名绕过 UNKNOWN。

每个外部操作有 5 秒硬截止时间；对账总窗口最多 30 秒，无无限 POST/DELETE
重试。收到明确订单 ID 后核对身份与 LIVE 状态，并立即只撤此 ID；后续读取
失败仍尽力撤已知自有 ID。`PASS` 必须同时具备 LIVE 观察、明确撤单 ACK、真实
终态、零成交及资金/选定 token 持仓核对。仅收到撤单 ACK 不够。确认成交时
报告 `PARTIAL`/`FILLED` 及数量/名义额，保留订单字段与成交记录的差异；持仓
变化但无成交证据时报告 `UNKNOWN`，不凭持仓差额虚构成交或损失。

进程崩溃、超时、缺订单 ID、终态或资金事实不完整均保留 `UNKNOWN`。无明确 ID
时，用户须直接查看交易所并核对账户；工具不按价格/时间猜 ID、不全撤、不卖出、
不重下。退出码 0 表示 `CHECKED` 或已完成核对的 `PASS`；拒绝、部分/全部成交、
阻断及 UNKNOWN 返回 2，仍须读取 JSON 结果区别处理。CLI 使用 POSIX 截止时间，
只适用于独立主线程进程，不应嵌入 Shadow 线程。当前验证仅为合成凭据和阻断真实
网络的 SDK HTTP 边界测试、离线入口及 shell 语法检查；不构成东京交易、部署、
资源容量、跨主机锁或地域可交易证据。

## 6. 门禁传输前提与正式部署

两主机门禁从本地接受的 detached release 执行；本地 Python 3.12 环境和
现有 Chrome、Python Playwright、缓存 Chromium、JS runner 必须实际可用。
门禁不安装浏览器，不自动启动 fixture server。

### 非 root SSH 的 bounded sudo adapter

正式远端配置 root-owned 0600，直接以 ubuntu 跑远端 gate 不能读它。本次
用了**操作员临时 adapter**，不是内置自动化，也不修改 gate/config 权限。
可复现做法是在私有本地目录放置名为 `ssh` 的 wrapper，仅在门禁命令的 PATH
前置它：按所选 gate 的每次调用核对完整 argv，仅允许下列两个固定选项变体、
固定任务 alias，以及当前发布的两条精确远端命令之一：

- `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no -o ConnectTimeout=10`
- `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10`

第二个变体未在 CLI 指定 ForwardAgent 时，已核对的专属 alias 仍必须设置
`ForwardAgent no`。所选发布源码核对中，两次 SSH 调用均使用第一个变体。
支持第二个变体的临时 adapter 也须核对实际执行的脚本，不能声称单一 argv
已覆盖未经核对的调用，也不能放宽到任意 SSH 选项或远端命令。

```text
cd /opt/open-trader/releases/<SHA> && env PYTHONPATH=/opt/open-trader/releases/<SHA>/src PYTHONDONTWRITEBYTECODE=1 /opt/open-trader/venvs/<SHA>/bin/python -m open_trader.prediction_cloud preflight --config /etc/open-trader/prediction-cloud.json
cd /opt/open-trader/releases/<SHA> && env PYTHONPATH=/opt/open-trader/releases/<SHA>/src PYTHONDONTWRITEBYTECODE=1 /opt/open-trader/venvs/<SHA>/bin/python -m open_trader.prediction_cloud smoke --config /etc/open-trader/prediction-cloud.json
```

命令以当前 `scripts/prediction-cloud-gate.py` 的 shlex 构造为准，精确比较，
不只检查 prefix/substring。匹配后仅将最后一个 argv 改为
`sudo -n bash -c ` 加 `shlex.quote(original_command)`，调用真实绝对路径
`/usr/bin/ssh`；不匹配即拒绝。不要添加 install/start、任意 root shell
或凭据命令。用已验证的绝对 Python shebang，先检查可执行文件和解释器；本次
临时工具曾因不存在的 Python shebang 失败。adapter 仅用于这两项门禁，
不放入客户端全局 PATH，不改变 sudoers 或授予新 root 权限。保留其 redacted
代码/匹配命令与退出状态在私有操作记录中，结束后退出该临时 PATH。

### wrapper 读取真实 GitHub CI 的前提

云端 wrapper 每次 install/start 必须可通过 `gh api` 读取真实仓库、main、
CI/checks 和 artifacts。可选择实际已认证的云端 `gh`（核对执行 wrapper 的
用户身份能只读访问；不要把 token 写入配置、命令或日志）。缺少认证则 BLOCKED，
不能把手写 proof JSON 当成 CI。

本次为避免向云端复制 GitHub token，Main 用临时 CI bridge：本地已有认证
`gh`，loopback-only HTTP listener，经专属 reverse SSH tunnel 暴露到远端
loopback。只允许 `repos/raymizzou/open_trader/` 的 main ref、actions、
check-suites 和 compare GET；远端临时 `gh` adapter 仅接受 preflight 的固定
host、GET、headers 和目标。成功响应原字节透传，包含 binary artifact，
不缓存/改写证据；错误返回非零，不制造成功。两个 listener 不绑公网，无
token 导出或 agent forwarding，日志只记目标/状态/字节数。部署后已关闭。
若选择此路线，操作员需先准备、检查并留存这些临时 adapter 和 tunnel 的
身份/范围，再执行正式 wrapper；它们不是仓库自带能力。本 SOP 默认采用
上述真实认证 gh 前提，不新增部署工具。任一路线不可用即停止。

### 两主机 READY → wrapper install/start → client → Smoke

保存云配置的非秘密本地副本及 operator evidence。Shadow evidence 含
`git_sha`、`independent_runtime_root`、真实 `resources_reviewed: true` 及带
时间/位置的 `resources_reviewed_evidence`。只有实际核查才可填 true；不要给
无凭据 Shadow 捏造 old_owner_stopped。SSM/生产另有 isolation/owner 要求。

本地新建私有 client runtime，准备 client JSON（0600；示例路径须替换）：

```json
{
  "release_root": "<absolute-local-detached-release>",
  "runtime_root": "<absolute-private-client-runtime>",
  "python": "<absolute-local-lock-matched-python3.12>",
  "ssh_alias": "open-trader-tokyo-shadow",
  "expected_sha": "<accepted-40-hex-main-sha>",
  "mode": "shadow",
  "gateway_port": 8876,
  "tunnel_port": 8879
}
```

在本地接受的 release 中，指定实际 Python、浏览器 runtime 和私有 evidence
目录。将 pytest cache/basetemp 指向 release 外，避免污染不可变源码。

```sh
export PYTHONDONTWRITEBYTECODE=1
export OPEN_TRADER_PYTHON='<absolute-local-release-venv-python>'
export PYTEST_ADDOPTS='-o cache_dir=<private-evidence-dir>/pytest-cache --basetemp=<private-evidence-dir>/pytest-temp'
make prediction-cloud-host-readiness \
  PYTHON_BIN="$OPEN_TRADER_PYTHON" REPOSITORY_ROOT='<local-browser-runtime>' \
  CLOUD_CLIENT_CONFIG='<private-client-json>' \
  CLOUD_SERVICE_CONFIG='<private-local-service-json>' \
  CLOUD_REMOTE_CONFIG=/etc/open-trader/prediction-cloud.json \
  CLOUD_OPERATOR_EVIDENCE='<private-operator-evidence-json>'
```

必须得到完整 `READY` 后才能 forward install/start。在远端 root 操作会话中，
继续使用第 3 节变量与实际 CI 传输 PATH：

```sh
cd "$RELEASE"
export OPEN_TRADER_PYTHON="$VENV/bin/python"
"$OPEN_TRADER_PYTHON" -B scripts/deploy_release.py --expected-sha "$SHA" \
  --release-root "$RELEASE" --runtime-root "$RUNTIME" --python "$OPEN_TRADER_PYTHON" \
  prediction-systemd --config /etc/open-trader/prediction-cloud.json --action install
"$OPEN_TRADER_PYTHON" -B scripts/deploy_release.py --expected-sha "$SHA" \
  --release-root "$RELEASE" --runtime-root "$RUNTIME" --python "$OPEN_TRADER_PYTHON" \
  prediction-systemd --config /etc/open-trader/prediction-cloud.json --action start
bash scripts/prediction-systemd.sh status
```

开始资源采样应在 start 前。默认 MemoryMax 768 MiB；`memory_max_bytes`
接受大于 64 MiB 的有限整数 bytes，不再固定最高 1 GB。已批准的 2 GiB 配置
为 `"memory_max_bytes": 2147483648`；所选发布必须包含该配置支持。
paused Shadow 固定 CPUQuota=100%（一核）、TasksMax=96、Restart=no；启动
要求 MemAvailable 至少预算加 350 MiB、swap used=0，并核对实际 kernel/cgroup
限制。guard 保留 64 MiB 服务余量；接近上限、低可用内存、swap、memory event
或不可读证据时只停止自身，不自动重试或信号其他服务。

本地使用明确 config，避免误用默认生产客户端：

```sh
bash scripts/prediction-client.sh start --config '<private-client-json>'
bash scripts/prediction-client.sh status --config '<private-client-json>'
make prediction-cloud-smoke \
  PYTHON_BIN="$OPEN_TRADER_PYTHON" REPOSITORY_ROOT='<local-browser-runtime>' \
  CLOUD_CLIENT_CONFIG='<private-client-json>' \
  CLOUD_SERVICE_CONFIG='<private-local-service-json>' \
  CLOUD_REMOTE_CONFIG=/etc/open-trader/prediction-cloud.json \
  CLOUD_OPERATOR_EVIDENCE='<private-operator-evidence-json>'
```

浏览器访问 `http://127.0.0.1:8876/`；隧道为本地 8879 → 云端 loopback 8769。
要求完整 `HEALTHY`；wrapper 0、RUNNING、HTTP 200 或 BACKEND_SMOKE_OK 都不
代替它。认证 profile 的 Smoke 还需真实新鲜背景账户/display 数据，不能沿用
disabled profile 的 LP 503 验收。不要扰动原 Mac 生产 8766/8769。

## 7. 启动检查、重连与容量验收

历史发布 `2797cd5` 的认证 Shadow 使用当时 1,000,000,000 bytes 预算，
在 2026-10-09 23:57:21+08:00 被该发布的资源 guard 以
`service_memory_headroom` 停止。采样 current=935,288,832 bytes，effective
limit=999,997,440 bytes；host MemAvailable=2,733,596,672 bytes、swap_used=0，
kernel memory failures=0。随后 systemd ActiveState=failed、无主进程，
Result=signal、ExecMainStatus=9、NRestarts=0。这是服务预算保护及其后强制终止，
不是 4 GB 主机耗尽 RAM、kernel OOM 或 agent 回滚的证据；未重试或重启。
原本地生产 owner 不变，无订单提交，已批准的 1 USDC 测试上限尚未使用。
metadata transport 错误与服务内存预算触发是两个独立未验收事项。移除固定
1 GB 上限及配置 2 GiB 已获批准，无需为同一变更再次申请批准；配置支持不
证明云端已部署或健康，也不证明完整启动与长期容量验收通过。未来无关的
代码/行为变更仍须另行提出具体方案并获批；不重试到绿。

远端检查实际 PID、`/proc/<PID>/cwd`/命令身份、root/code_root/SHA、mode、
runtime lock、release record、8769 loopback listener、N-leg pause、
credential profile、candidate_exclusions 和从启动时间起的日志。不要输出含
秘密的完整进程环境。以下只取管理与资源字段：

```sh
systemctl show open-trader-prediction.service \
  -p MainPID -p ActiveState -p UnitFileState -p Restart -p NRestarts \
  -p MemoryCurrent -p MemoryPeak -p MemoryMax -p CPUQuotaPerSecUSec \
  -p TasksCurrent -p TasksMax -p ControlGroup
ss -ltnp
journalctl -u open-trader-prediction.service --since '<actual-start-time>'
CG=$(systemctl show open-trader-prediction.service -p ControlGroup --value)
test -n "$CG"
cat "/sys/fs/cgroup$CG/memory.current" "/sys/fs/cgroup$CG/memory.peak" \
  "/sys/fs/cgroup$CG/memory.events"
free -h
swapon --show
```

仅记录脱敏日志；gate 已检查 identity/locks，但手工交接仍要保留结果。
资源测量用整个 cgroup（含子进程和 file cache），不能用 RSS 代替。
认证启动验收需完整 catalog/metadata/history 准备和一次真实完整 candidate
遍历，再确认账户/display 持续新鲜；`candidate_pending_count=0` 单独不足。
之后才开始至少 24 小时观测，覆盖 12 小时 metadata refresh 及其完整处理。
要求无 OOM/restart/swap pressure、管理仍可达、完整性/新鲜度不变。
不裁剪市场池或备份队列以满足预算。阻断/guard stop/UNKNOWN 保留证据并结束
本次尝试，修复后需新的验证，不能重试到绿。

Shadow 的 Restart=no、boot disabled 是实验生命周期设计，不手工 enable。
主机重启/guard stop 后，先核查状态、资源、锁及所选证据，刷新 READY，再走
已授权的正式 wrapper start 和 Smoke。CI 过期时停止 forward 操作，不自动
重跑全测试。停止云端仅在授权范围用远端 `prediction-systemd.sh stop`，
并验证自身 listener/lock 消失；它不会取消交易所订单。

本地断线先检查该 client runtime 的 gateway.log/ssh.log，再用**原保存配置**：

```sh
bash scripts/prediction-client.sh stop --config '<private-client-json>'
bash scripts/prediction-client.sh start --config '<private-client-json>'
bash scripts/prediction-client.sh status --config '<private-client-json>'
```

这些命令只管理已记录 Gateway/SSH，不重启云后端。关浏览器/stop client 不停
云服务。占用端口或 PID 身份不明时停止，不能杀未知进程。失败不自动 rollback；
保留旧 immutable source/venv 和失败 runtime，按既有授权恢复程序处理。
稳定 lpauto installer 当前仅支持 macOS，不承诺云端已具备同类安装器。

## 8. 持久交接清单

在公开 Git 外保存主机专属 operator inventory 和脱敏证据，不依赖临时目录：

- 精确 release SHA、main-push CI run/attempt、artifact 有效期、preflight 结果。
- 云端 source/venv/runtime/config 路径、user/group、owner/mode、mode/backend；
  凭据只记文件路径及权限，不记内容、钱包地址或 Keychain 导出。
- 本地 source/python/client runtime/client config/browser runtime、任务 alias；
  私钥路径和主机地址只在私有 inventory，记录 host-key 核对方式。
- 所选端口、原生产 owner 未受影响的身份检查；READY/install/start/Smoke 的
  SHA、配置、时间、退出状态、结果和可长期访问的脱敏证据位置。
- PID、启动时间、根路径、lock/listener、unit 实际限制、资源序列和范围；
  adapter/CI bridge 若使用，记录实际版本、允许范围、关闭结果。
- 已完成认证读取/新鲜 Dashboard 与待完成 Smoke/完整遍历/24h/12h-refresh/订单事项分别列出；
  geoblock 冲突和原因仍未知，待批准方案的 owner 与下一步明确记载。

新会话必须重新读 inventory 与所选发布 runbook，刷新外部/主机状态。不能把
本次历史 `HEALTHY`、旧 SHA 或临时 helper 的存在当作当前成功。
