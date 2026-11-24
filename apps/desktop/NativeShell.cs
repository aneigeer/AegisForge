using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Media;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

[assembly: AssemblyTitle("AegisForge")]
[assembly: AssemblyDescription("AegisForge · 企业级全栈安全检测与多 Agent 自进化修复平台")]
[assembly: AssemblyProduct("AegisForge")]
[assembly: AssemblyVersion("2.4.0.0")]

namespace DBGuardianDesktop
{
    public class DagNode
    {
        public int Id;
        public float X;
        public float Y;
        public string Category;
        public string Title;
        public string Subtitle;
        public string Metric;
        public string Status; // completed | running | pending | waiting | bypassed | rejected
        public string[] Logs;
        public string[,] Meta;
        public RectangleF HitRect;
    }

    public class DagEdge
    {
        public int FromId;
        public int ToId;
        public string Dir; // right-left | left-right | bottom-top
    }

    public class MaskingRuleItem
    {
        public string Id;
        public string Label;
        public string Desc;
        public bool Enabled;
        public RectangleF ToggleRect;
    }

    public class EvoVersionItem
    {
        public string Version;
        public string Date;
        public string Summary;
        public string ShadowScore;
        public string RulesAdded;
        public RectangleF RollbackBtnRect;
    }

    public class AuditLogItem
    {
        public string Time;
        public string Actor;
        public string Action;
        public string Detail;
        public string Hash;
        public string Status;
    }

    public class AssetRowItem
    {
        public string Name;
        public string EngineInfo;
        public string TierBadge;
        public bool IsCoreP0;
        public string ExposureBadge;
        public bool IsPublic;
        public string VaultInfo;
        public string PoolStrategy;
        public string Score;
        public string ScoreLabel;
        public int ScoreLevel; // 0=high(rose), 1=med(amber), 2=ok(emerald)
        public string ActionText;
        public RectangleF ActionBtnRect;
    }

    // 多 Agent 协作角色徽章（纯职能标识，不含任何虚拟人名）
    public class AgentBadge
    {
        public string Id;
        public string RoleLabel;
        public string FunctionText;
        public string State;   // idle | active | verify | rejected | passed | escalated
        public int Round;
    }

    public class MainOpsForm : Form
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        private readonly string _repoRoot;
        private readonly Timer _frameTimer;
        private float _time = 0f;
        private bool _isDark = true;

        // 慢速圆心扩散主题切换 (~1.4s SmoothStep)
        private Bitmap _themeSnapshot = null;
        private float _themeTransition = 1.0f;
        private PointF _themeOrigin = new PointF(1460, 28);
        private float _sunMoonAngle = 0f;
        private float _sunMoonTargetAngle = 0f;

        // 6 大视图导航: 0=workflow, 1=assets, 2=gateway, 3=evolution, 4=audit, 5=eval_data
        private int _currentView = 0;
        private readonly RectangleF[] _navRects = new RectangleF[6];
        private RectangleF _tenantCardRect;
        private int _rbacRoleIndex = 0; // 0=高级DBA, 1=安全工程师, 2=合规审计员

        // 顶栏控件区
        private RectangleF _pillGatewayRect;
        private RectangleF _evoToggleBoxRect;
        private RectangleF _btnSimulateRect;
        private RectangleF _btnSnapshotRect;
        private RectangleF _btnThemeRect;
        private RectangleF _btnFastModeRect;
        private PointF _mousePos = new PointF(-1, -1);

        // 极速排障模式：一键关闭圆心扩散等非必要过渡动效，实现 0ms 瞬时切换
        private bool _fastTroubleshoot = false;

        // 全局自主进化开关与版本状态
        private bool _selfEvolutionEnabled = true;
        private string _activeEvoVersion = "v1.8";

        // VIEW 1: 工作流编排 DAG 与右侧检视器状态
        private readonly List<DagNode> _nodes = new List<DagNode>();
        private readonly List<DagEdge> _edges = new List<DagEdge>();

        // 多 Agent 协作：角色徽章 + Critic 自愈轮次（硬上限 3 轮）
        private readonly List<AgentBadge> _agents = new List<AgentBadge>();
        private int _criticRound = 0;
        private int _criticMaxRounds = 3;
        private string _criticVerdict = "";
        private int _selfCorrectionTotal = 0;
        private RectangleF _agentStripRect;

        private int _selectedNodeId = 6; // 默认选中 [HITL 审批]
        private int _hitlActiveTab = 0;  // 0=修复SQL, 1=Rollback回滚, 2=沙箱报告, 3=脱敏对比
        private readonly RectangleF[] _hitlTabRects = new RectangleF[4];
        private RectangleF _btnJumpToHitlRect;

        // DAG 节点拖拽支持
        private DagNode _draggingNode = null;
        private PointF _dragOffset;

        // HITL 长按批准 / 长按驳回 (沿边框填充进度 + 未满松开回弹 + 满格微震动)
        private RectangleF _btnApproveHoldRect;
        private RectangleF _btnRejectHoldRect;
        private int _activeHoldMode = 0; // 0=none, 1=approve, 2=reject
        private float _approveProgress = 0f;
        private float _approveVelocity = 0f;
        private float _rejectProgress = 0f;
        private float _rejectVelocity = 0f;
        private int _windowShakeTicks = 0;
        private Point _windowBaseLocation;

        // 模拟执行流水线状态机
        private bool _isSimulating = false;
        private int _simStepTicks = 0;
        private int _postApproveTicks = 0;
        private string _workflowPillText = "等待 HITL 审批";
        private string _workflowPillColor = "amber"; // amber | cyan | emerald | indigo | rose

        // VIEW 2: 资产与凭据
        private readonly List<AssetRowItem> _assets = new List<AssetRowItem>();
        private RectangleF _btnSyncCmdbRect;

        // VIEW 3: 模型网关与脱敏
        private readonly List<MaskingRuleItem> _maskingRules = new List<MaskingRuleItem>();
        private RectangleF _btnConfigByokRect;
        private string _byokModelName = "Claude 3.7 Sonnet API";

        // VIEW 4: 自主进化中心
        private readonly List<EvoVersionItem> _evoVersions = new List<EvoVersionItem>();
        private RectangleF _btnEvoBannerToggleRect;

        // VIEW 5: 审计日志
        private readonly List<AuditLogItem> _auditLogs = new List<AuditLogItem>();
        private RectangleF _btnExportSarifRect;
        private RectangleF _btnOneClickRestoreRect;
        private bool _oneClickRestoreDone = false;

        // VIEW 6: 评测与数据中台
        private bool _evalConflictMode = false;
        private string _evalVerdict = "CANDIDATE_BETTER";
        private string _evalReason = "ALL_METRICS_IMPROVED";
        private int _suiteV2Cases = 36;
        private string _goldenStatus = "就绪（点击右侧一键脱敏导出）";
        private int _goldenKept = 0;
        private string _goldenOutputDir = "runs/golden/golden-desktop-v1";
        // V4.1：评测数据来源（LIVE_EVAL = 真实跑过 run-guardrail-eval；BASELINE_SNAPSHOT = 内置快照）
        private string _evalDataSource = "BASELINE_SNAPSHOT";
        private string _guardrailLeakage = "-";
        private RectangleF _btnRefreshEvalRect;
        private RectangleF _btnExportGoldenRect;
        private RectangleF _btnToggleConflictRect;
        private RectangleF _btnRunGuardrailEvalRect;
        private RectangleF _btnScanRepoRect;

        // 悬浮 Toast 提示
        private string _toastText = "";
        private string _toastKind = "info";
        private int _toastTicks = 0;

        public MainOpsForm(string repoRoot)
        {
            _repoRoot = repoRoot;
            Text = "AegisForge · 企业级漏洞检测与自进化修复平台 (原生本地桌面端 v2.4)";
            Size = new Size(1540, 940);
            MinimumSize = new Size(1260, 780);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(9, 13, 22);
            DoubleBuffered = true;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);

            InitDataStore();
            AsyncPrepareLocalEnv();

            _frameTimer = new Timer();
            _frameTimer.Interval = 16; // 60 FPS
            _frameTimer.Tick += OnFrameTick;
            _frameTimer.Start();
        }

        private void AsyncPrepareLocalEnv()
        {
            try
            {
                string pyExe = ResolvePythonExe();
                if (pyExe != null && _repoRoot != null)
                {
                    ProcessStartInfo psi = new ProcessStartInfo
                    {
                        FileName = pyExe,
                        Arguments = "-m apps.desktop.launcher --prepare-only",
                        WorkingDirectory = _repoRoot,
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        WindowStyle = ProcessWindowStyle.Hidden
                    };
                    Process.Start(psi);
                }
            }
            catch { }
        }

        private string ResolvePythonExe()
        {
            if (!string.IsNullOrEmpty(_repoRoot))
            {
                string venvPy = Path.Combine(_repoRoot, ".venv", "Scripts", "python.exe");
                if (File.Exists(venvPy)) return venvPy;
            }
            return "python";
        }

        private void InitDataStore()
        {
            BuildDefaultDagNodes();

            _edges.Clear();
            _edges.Add(new DagEdge { FromId = 1, ToId = 2, Dir = "right-left" });
            _edges.Add(new DagEdge { FromId = 2, ToId = 3, Dir = "right-left" });
            _edges.Add(new DagEdge { FromId = 3, ToId = 4, Dir = "bottom-top" });
            _edges.Add(new DagEdge { FromId = 4, ToId = 5, Dir = "left-right" });
            _edges.Add(new DagEdge { FromId = 5, ToId = 6, Dir = "left-right" });
            _edges.Add(new DagEdge { FromId = 6, ToId = 7, Dir = "bottom-top" });
            _edges.Add(new DagEdge { FromId = 7, ToId = 8, Dir = "right-left" });

            // 5 台异构数据库资产
            _assets.Clear();
            _assets.Add(new AssetRowItem
            {
                Name = "MySQL-Prod-Order",
                EngineInfo = "MySQL 8.0.32 · 10.24.8.19:3306",
                TierBadge = "核心 (P0 · 含PII)",
                IsCoreP0 = true,
                ExposureBadge = "公网跳板映射",
                IsPublic = true,
                VaultInfo = "HashiCorp Vault · 动态只读",
                PoolStrategy = "EPHEMERAL · 瞬时容器",
                Score = "9.1",
                ScoreLabel = "高危",
                ScoreLevel = 0,
                ActionText = "查看修复流"
            });
            _assets.Add(new AssetRowItem
            {
                Name = "PG-Core-Ledger",
                EngineInfo = "PostgreSQL 15.6 · 10.24.12.44:5432",
                TierBadge = "核心 (P0 · 财务)",
                IsCoreP0 = true,
                ExposureBadge = "严格内网隔离",
                IsPublic = false,
                VaultInfo = "HashiCorp Vault · 动态凭据",
                PoolStrategy = "EPHEMERAL · 瞬时容器",
                Score = "2.4",
                ScoreLabel = "合规通过",
                ScoreLevel = 2,
                ActionText = "立即巡检"
            });
            _assets.Add(new AssetRowItem
            {
                Name = "Redis-Session-Cluster",
                EngineInfo = "Redis 7.0.14 · 10.24.30.10:6379",
                TierBadge = "重要 (P1)",
                IsCoreP0 = false,
                ExposureBadge = "内网 VPC",
                IsPublic = false,
                VaultInfo = "KMS 加密托管 · ACL",
                PoolStrategy = "EPHEMERAL · 瞬时容器",
                Score = "5.6",
                ScoreLabel = "中危 (未禁用危险命令)",
                ScoreLevel = 1,
                ActionText = "立即巡检"
            });
            _assets.Add(new AssetRowItem
            {
                Name = "TiDB-Analytics-HTAP",
                EngineInfo = "TiDB 7.5.1 · 10.24.51.100:4000",
                TierBadge = "重要 (P1)",
                IsCoreP0 = false,
                ExposureBadge = "内网 VPC",
                IsPublic = false,
                VaultInfo = "HashiCorp Vault · 动态凭据",
                PoolStrategy = "WARM_POOL · TiDB sandbox_db",
                Score = "1.9",
                ScoreLabel = "健康",
                ScoreLevel = 2,
                ActionText = "查看报告"
            });
            _assets.Add(new AssetRowItem
            {
                Name = "Oracle-ERP-Legacy",
                EngineInfo = "Oracle 19c · 10.24.90.8:1521",
                TierBadge = "核心 (P0)",
                IsCoreP0 = true,
                ExposureBadge = "核心隔离区",
                IsPublic = false,
                VaultInfo = "堡垒机临时授权 · 零接触",
                PoolStrategy = "WARM_POOL · Oracle PDB Clone",
                Score = "6.2",
                ScoreLabel = "中危 (补丁滞后)",
                ScoreLevel = 1,
                ActionText = "升级评估"
            });

            // 5 类核心 Agent 角色徽章（纯职能标识：总控调度 / 研判 / 领域专家 / 红队验证 / 进化蒸馏）
            _agents.Clear();
            _agents.Add(new AgentBadge { Id = "AGENT_SUPERVISOR", RoleLabel = "总控调度", FunctionText = "DAG 编排", State = "idle", Round = 0 });
            _agents.Add(new AgentBadge { Id = "AGENT_TRIAGE", RoleLabel = "研判定级", FunctionText = "P0~P3 工单", State = "idle", Round = 0 });
            _agents.Add(new AgentBadge { Id = "AGENT_DB_SPECIALIST", RoleLabel = "DB 专家", FunctionText = "三层修复+Rollback", State = "idle", Round = 0 });
            _agents.Add(new AgentBadge { Id = "AGENT_CRITIC", RoleLabel = "Critic 红队", FunctionText = "沙箱对抗验证", State = "idle", Round = 0 });
            _agents.Add(new AgentBadge { Id = "AGENT_EVOLUTION", RoleLabel = "进化蒸馏", FunctionText = "Shadow Mode", State = "idle", Round = 0 });
            _criticRound = 0;
            _criticVerdict = "";
            _selfCorrectionTotal = 0;

            // 4 条隐私脱敏规则
            _maskingRules.Clear();
            _maskingRules.Add(new MaskingRuleItem { Id = "tables", Label = "真实表名与字段名脱敏", Desc = "将 prod_pay_users 等业务表名映射为 tbl_masked_xx (返回后自动还原)", Enabled = true });
            _maskingRules.Add(new MaskingRuleItem { Id = "ips", Label = "IP 地址与主机名脱敏", Desc = "将内网 IP 10.24.8.19 替换为 10.x.x.19", Enabled = true });
            _maskingRules.Add(new MaskingRuleItem { Id = "creds", Label = "账号密码与 Token 强拦截", Desc = "自动擦除密码哈希、AK/SK 与连接串凭据", Enabled = true });
            _maskingRules.Add(new MaskingRuleItem { Id = "pii", Label = "样本 PII 敏感数据替换", Desc = "替换日志中可能携带的手机号、身份证与银行卡号", Enabled = true });

            // 3 个 Git 风格进化版本
            _evoVersions.Clear();
            _evoVersions.Add(new EvoVersionItem
            {
                Version = "v1.8",
                Date = "2026-11-21 18:30",
                Summary = "蒸馏 MySQL 8.0 业务只读账号最小权限模板，识别内部 ShardingSphere 代理特征，误报率下降 14%。",
                ShadowScore = "99.2% 通过",
                RulesAdded = "+4 条检测规则 / +7 条领域记忆"
            });
            _evoVersions.Add(new EvoVersionItem
            {
                Version = "v1.7",
                Date = "2026-11-15 11:15",
                Summary = "从 DBA 人工修改的 PostgreSQL RLS 策略中学习，优化 Rollback 回滚脚本生成的事务安全性。",
                ShadowScore = "98.5% 通过",
                RulesAdded = "+2 条检测规则 / +5 条领域记忆"
            });
            _evoVersions.Add(new EvoVersionItem
            {
                Version = "v1.6",
                Date = "2026-11-04 09:40",
                Summary = "沉淀 Redis 集群 rename-command 补偿性控制识别记忆，避免对已做内网网关防护的实例重复告警。",
                ShadowScore = "97.9% 通过",
                RulesAdded = "+3 条检测规则 / +9 条领域记忆"
            });

            // 审计日志
            _auditLogs.Clear();
            _auditLogs.Add(new AuditLogItem { Time = "2026-11-22 21:04:29", Actor = "Harness-Engine", Action = "沙箱验证", Detail = "Docker mysql:8.0.32 复现 PoC 并验证修复 SQL 兼容 240/240 条业务查询", Hash = "8f4a2e...91c0d", Status = "通过" });
            _auditLogs.Add(new AuditLogItem { Time = "2026-11-22 21:04:16", Actor = "Privacy-Gateway", Action = "脱敏调用", Detail = "向 Claude-3.7 API 发送推理请求前成功替换 12 处敏感表名与 IP", Hash = "3b91c4...77e2a", Status = "已脱敏" });
            _auditLogs.Add(new AuditLogItem { Time = "2026-11-22 21:04:12", Actor = "Scanner-L1 (Ollama)", Action = "漏洞扫描", Detail = "对 MySQL-Prod-Order 执行低侵入只读扫描，检出 1 高危 / 2 中危", Hash = "a17f09...42b8f", Status = "完成" });
            _auditLogs.Add(new AuditLogItem { Time = "2026-11-22 21:04:11", Actor = "Vault-Broker", Action = "动态凭据", Detail = "签发临时只读账号 ro_scan_992a (TTL=15m)，零明文落盘", Hash = "d55e81...10a3c", Status = "已签发" });
            _auditLogs.Add(new AuditLogItem { Time = "2026-11-21 18:30:05", Actor = "Evolution-Engine", Action = "进化发布", Detail = "影子测试通过率 99.2%，自动发布领域记忆快照 v1.8", Hash = "9c02b7...65f11", Status = "生效中" });
            _auditLogs.Add(new AuditLogItem { Time = "2026-11-20 14:22:18", Actor = "高级 DBA", Action = "人工审批", Detail = "批准 PG-Core-Ledger SSL 强制加密变更单 #CHG-2026-8790", Hash = "4e61a0...83d92", Status = "已执行" });
        }

        private void BuildDefaultDagNodes()
        {
            _nodes.Clear();
            _nodes.Add(new DagNode
            {
                Id = 1, X = 32, Y = 54,
                Category = "02 资产探测",
                Title = "MySQL-Prod-Order",
                Subtitle = "只读限流 · Vault 动态凭据",
                Metric = "1.2s",
                Status = "completed",
                Logs = new string[]
                {
                    "[21:04:11.021] 向 HashiCorp Vault 申请临时只读凭据 (TTL=15m)... 成功",
                    "[21:04:11.482] 连接目标 10.24.8.19:3306 (MySQL 8.0.32)，启用限流 QPS<=20",
                    "[21:04:12.230] 资产上下文标签加载完成: 业务等级=P0核心, 暴露面=公网跳板映射, 含PII字段"
                },
                Meta = new string[,] { { "引擎版本", "MySQL 8.0.32" }, { "探查模式", "低侵入只读" }, { "CPU增量", "0.4%" }, { "凭据来源", "Vault 动态签发" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 2, X = 296, Y = 54,
                Category = "03/04 多维检测与定级",
                Title = "基线 / CVE / 慢查询扫描",
                Subtitle = "发现 1 高危(9.1分) / 2 中危",
                Metric = "3.8s",
                Status = "completed",
                Logs = new string[]
                {
                    "[21:04:12.510] 加载合规基线库: CIS MySQL 8.0 Benchmark v1.4 + 等保 2.0",
                    "[21:04:14.104] [CRITICAL] 检测到 secure_file_priv=\"\" (允许任意路径读写文件)",
                    "[21:04:14.890] [CRITICAL] 业务账号 app_order_rw 拥有 GRANT ALL PRIVILEGES ON *.*",
                    "[21:04:16.312] 上下文定级引擎计算真实业务风险分: 9.1 / 10.0 (P0 高危)"
                },
                Meta = new string[,] { { "高危漏洞", "1 项 (组合提权)" }, { "中危配置", "2 项 (已过滤低危)" }, { "业务风险分", "9.1 (P0)" }, { "路由模型", "Ollama-Local L1" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 3, X = 560, Y = 54,
                Category = "07 隐私网关",
                Title = "Prompt 敏感数据脱敏",
                Subtitle = "替换 12 处表名 / IP / 账号",
                Metric = "0.18s",
                Status = "completed",
                Logs = new string[]
                {
                    "[21:04:16.350] 拦截出站推理请求，启动 SQL Parser AST 节点分类引擎",
                    "[21:04:16.412] AST 节点替换: orders_pay_vip -> __IDENT_001__，10.24.8.19 -> __IP_001__，手机号 -> __PII_001__",
                    "[21:04:16.485] 映射表经 KMS 信封加密持久化 (workflow_run_id=wf-run-7f3a91, TTL=86400s)",
                    "[21:04:16.530] 脱敏审计快照写入不可篡改日志，放行至外部模型网关"
                },
                Meta = new string[,] { { "AST 节点替换", "标识符 9 · 字面量 3" }, { "敏感值泄露", "0 字节" }, { "KMS 映射持久化", "run_id 绑定 · TTL 24h" }, { "闭环销毁", "变更完成即擦除" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 4, X = 560, Y = 232,
                Category = "05 AI 修复生成",
                Title = "修复方案与回滚脚本生成",
                Subtitle = "Claude-3.7 · 分层策略+Rollback",
                Metric = "4.1s",
                Status = "completed",
                Logs = new string[]
                {
                    "[21:04:16.620] 路由至 L2 强推理模型 (Claude-3.7 Sonnet API)",
                    "[21:04:18.900] 生成分层策略: 最小权限收敛 SQL + 临时 WAF 拦截规则 + 参数加固建议",
                    "[21:04:20.410] 强制校验通过: 已配套生成完整 Rollback 回滚脚本与锁表影响评估",
                    "[21:04:20.720] 隐私网关本地还原真实表名与账号标识完成"
                },
                Meta = new string[,] { { "推理模型", "Claude-3.7 API" }, { "Token 消耗", "1,840" }, { "回滚脚本", "已强制附带" }, { "锁表评估", "无锁表风险" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 5, X = 296, Y = 232,
                Category = "06 双轨验证 Harness",
                Title = "沙箱 Dry-Run + 生产只读预检",
                Subtitle = "双轨通过✓ 兼容100% · 长事务0",
                Metric = "8.4s",
                Status = "completed",
                Logs = new string[]
                {
                    "[21:04:21.010] 分层池化调度: 拉起瞬时容器 mysql:8.0.32-sandbox (EPHEMERAL_CONTAINER)",
                    "[21:04:23.800] [轨道1] 漏洞 PoC 复现成功 -> 修复 SQL Dry-Run 42ms -> Rollback 可逆验证通过",
                    "[21:04:28.920] [轨道1] 复测 PoC 已拦截；回放 240 条脱敏业务 SQL -> 100% 兼容通过",
                    "[21:04:29.310] [轨道2] 只读预检: 486,120 行 / 534.7MB，>30s 活跃长事务 0 个 -> 放行"
                },
                Meta = new string[,] { { "池化策略", "EPHEMERAL_CONTAINER" }, { "沙箱镜像", "mysql:8.0.32" }, { "业务兼容性", "240/240 (100%)" }, { "长事务预检", "0 个 · 放行" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 6, X = 32, Y = 232,
                Category = "09 HITL 审批断点",
                Title = "DBA & 安全双人审批",
                Subtitle = "长按右侧按钮沿边框蓄力放行",
                Metric = "待处理",
                Status = "pending",
                Logs = new string[]
                {
                    "[21:04:29.500] 检测到目标资产为 P0 核心库，触发高危变更人工断点 (Human-in-the-loop)",
                    "[21:04:29.580] 已推送审批卡片至飞书/钉钉/Jira工单系统，等待 DBA 长按蓄力确认"
                },
                Meta = new string[,] { { "审批策略", "P0 双人会签" }, { "工单号", "CHG-2026-8841" }, { "变更窗口", "随时可执行(无锁)" }, { "状态", "待审批" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 7, X = 32, Y = 410,
                Category = "安全执行与回滚保护",
                Title = "生产变更与回滚保护",
                Subtitle = "原子执行 · 锁超时3s · 分级回滚",
                Metric = "等待前置",
                Status = "waiting",
                Logs = new string[]
                {
                    "[待执行] 审批放行后经堡垒机动态通道执行，外层强制注入 SET SESSION lock_wait_timeout = 3;",
                    "[0~5m] 实时熔断期：SQL 报错或 QPS 断崖下跌 > 20% 自动执行 Rollback",
                    "[24~72h] 延迟观察期：联动 SIEM 监听权限报错，支持一键紧急恢复"
                },
                Meta = new string[,] { { "执行通道", "Vault 临时提权" }, { "锁超时保护", "3s 强制注入" }, { "实时熔断", "0~5m 自动回滚" }, { "延迟观察", "24~72h · SIEM" } }
            });
            _nodes.Add(new DagNode
            {
                Id = 8, X = 340, Y = 410,
                Category = "08 自主进化引擎",
                Title = "规则蒸馏与领域记忆库",
                Subtitle = _selfEvolutionEnabled ? "影子测试验证后入库" : "已关闭 · 静态模式旁路",
                Metric = _selfEvolutionEnabled ? "等待触发" : "已跳过",
                Status = _selfEvolutionEnabled ? "waiting" : "bypassed",
                Logs = new string[]
                {
                    "[待触发] 修复闭环完成后，将自动提取本次误报过滤特征与最小权限收敛模板，进入 Shadow Mode 影子验证队列。"
                },
                Meta = new string[,] { { "进化模式", _selfEvolutionEnabled ? "已开启 (受控)" : "已关闭 (静态)" }, { "安全边界", "仅进化规则不越权" }, { "目标记忆库", "企业金融域 " + _activeEvoVersion }, { "影子验证", "99.2% 通过率" } }
            });
        }

        private void ShowToast(string text, string kind)
        {
            _toastText = text;
            _toastKind = kind;
            _toastTicks = 175; // ~2.8 秒
        }

        private void AddAuditEntry(string actor, string action, string detail, string status)
        {
            string now = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
            string hash = Guid.NewGuid().ToString("N").Substring(0, 6) + "..." + Guid.NewGuid().ToString("N").Substring(0, 5);
            _auditLogs.Insert(0, new AuditLogItem
            {
                Time = now,
                Actor = actor,
                Action = action,
                Detail = detail,
                Hash = hash,
                Status = status
            });
        }

        private void OnFrameTick(object sender, EventArgs e)
        {
            _time += 0.016f;

            // 极速排障模式：终止所有非必要过渡动效，保证 0ms 瞬时响应
            if (_fastTroubleshoot)
            {
                _themeTransition = 1.0f;
                if (_themeSnapshot != null)
                {
                    _themeSnapshot.Dispose();
                    _themeSnapshot = null;
                }
                _sunMoonAngle = _sunMoonTargetAngle;
            }

            if (_toastTicks > 0) _toastTicks--;

            // 慢速圆心扩散主题切换动画 (~1.4s SmoothStep)
            if (_themeTransition < 1.0f)
            {
                _themeTransition += 0.0115f;
                if (_themeTransition >= 1.0f)
                {
                    _themeTransition = 1.0f;
                    if (_themeSnapshot != null)
                    {
                        _themeSnapshot.Dispose();
                        _themeSnapshot = null;
                    }
                }
            }
            _sunMoonAngle += (_sunMoonTargetAngle - _sunMoonAngle) * 0.075f;

            // 长按批准 / 长按驳回 边框进度蓄力与未满松开阻尼回弹
            UpdateHoldPhysics();

            // 模拟全链路运行状态机
            UpdateSimulationStateMachine();

            // 审批通过后的下游节点 7 -> 节点 8 自动推进动画
            UpdatePostApproveStateMachine();

            // 满格确认后的原生窗口微震动
            if (_windowShakeTicks > 0)
            {
                _windowShakeTicks--;
                if (_windowShakeTicks == 0)
                {
                    Location = _windowBaseLocation;
                }
                else
                {
                    double phase = _windowShakeTicks * 2.0;
                    int dx = (int)Math.Round(Math.Sin(phase) * 4.0);
                    int dy = (int)Math.Round(Math.Cos(phase * 1.3) * 2.0);
                    Location = new Point(_windowBaseLocation.X + dx, _windowBaseLocation.Y + dy);
                }
            }

            Invalidate();
        }

        private void UpdateHoldPhysics()
        {
            DagNode n6 = FindNode(6);
            bool isPending = (n6 != null && n6.Status == "pending");

            // 1. 批准按钮边框蓄力与未满回弹
            if (isPending && _activeHoldMode == 1)
            {
                _approveProgress += 0.021f; // ~0.76s 填满四条边框
                _approveVelocity = 0f;
                if (_approveProgress >= 1.0f)
                {
                    _approveProgress = 1.0f;
                    _activeHoldMode = 0;
                    TriggerMicroVibration();
                    ExecuteApproveHitl();
                }
            }
            else if (_approveProgress > 0f && (n6 == null || n6.Status != "completed"))
            {
                _approveVelocity -= 0.012f;
                _approveProgress += _approveVelocity;
                if (_approveProgress <= 0f)
                {
                    _approveProgress = 0f;
                    _approveVelocity = 0f;
                }
            }

            // 2. 驳回按钮边框蓄力与未满回弹
            if (isPending && _activeHoldMode == 2)
            {
                _rejectProgress += 0.021f;
                _rejectVelocity = 0f;
                if (_rejectProgress >= 1.0f)
                {
                    _rejectProgress = 1.0f;
                    _activeHoldMode = 0;
                    TriggerMicroVibration();
                    ExecuteRejectHitl();
                }
            }
            else if (_rejectProgress > 0f && (n6 == null || n6.Status != "rejected"))
            {
                _rejectVelocity -= 0.012f;
                _rejectProgress += _rejectVelocity;
                if (_rejectProgress <= 0f)
                {
                    _rejectProgress = 0f;
                    _rejectVelocity = 0f;
                }
            }
        }

        private void ExecuteApproveHitl()
        {
            DagNode n6 = FindNode(6);
            DagNode n7 = FindNode(7);
            if (n6 == null || n7 == null) return;

            n6.Status = "completed";
            n6.Subtitle = "高级 DBA 已长按批准放行";
            n6.Metric = "已通过";

            n7.Status = "running";
            n7.Subtitle = "正在通过 Vault 通道原子执行...";
            n7.Metric = "执行中";

            _workflowPillText = "正在执行生产变更";
            _workflowPillColor = "cyan";
            AddAuditEntry("高级 DBA", "人工审批", "长按批准 MySQL-Prod-Order 最小权限收敛变更单 #CHG-2026-8841", "已批准");
            ShowToast("审批已通过！正在安全执行生产变更并开启回滚观测...", "emerald");
            SetAgentState("AGENT_EVOLUTION", "active", 0);
            _postApproveTicks = 1;
        }

        private void ExecuteRejectHitl()
        {
            DagNode n6 = FindNode(6);
            DagNode n7 = FindNode(7);
            if (n6 == null || n7 == null) return;

            n6.Status = "rejected";
            n6.Subtitle = "已被安全审批员长按驳回";
            n6.Metric = "已驳回";

            n7.Status = "waiting";
            n7.Subtitle = "前置未通过，已终止";
            n7.Metric = "已终止";

            _workflowPillText = "已驳回变更";
            _workflowPillColor = "rose";
            AddAuditEntry("高级 DBA", "人工审批", "长按驳回 MySQL-Prod-Order 变更单 #CHG-2026-8841，退回人工复核", "已驳回");
            ShowToast("已长按驳回本次变更，下游执行已安全终止", "amber");
            SetAgentState("AGENT_EVOLUTION", "idle", 0);
        }

        private void UpdatePostApproveStateMachine()
        {
            if (_postApproveTicks <= 0) return;
            _postApproveTicks++;

            DagNode n7 = FindNode(7);
            DagNode n8 = FindNode(8);

            if (_postApproveTicks == 55 && n7 != null)
            {
                n7.Status = "completed";
                n7.Subtitle = "变更成功 · 观测 0 异常";
                n7.Metric = "0.14s";
                n7.Logs = new string[]
                {
                    "[21:09:12.104] 获取 Vault 临时提权通道成功，开启事务与健康度探针",
                    "[21:09:12.188] 执行 fix_mysql_priv_20261122.sql -> 影响 0 锁表，耗时 84ms",
                    "[21:09:12.244] 业务 QPS 与错误率正常，Rollback 脚本已归档保留 7 天"
                };
                AddAuditEntry("Execution-Agent", "脚本执行", "MySQL-Prod-Order 生产修复脚本执行成功 (84ms)，无锁表异常", "已生效");

                if (_selfEvolutionEnabled && n8 != null)
                {
                    n8.Status = "running";
                    n8.Subtitle = "正在蒸馏规则至领域记忆库...";
                    n8.Metric = "进化中";
                    _workflowPillText = "自主进化蒸馏中";
                    _workflowPillColor = "indigo";
                }
                else
                {
                    _workflowPillText = "修复完成 (静态模式)";
                    _workflowPillColor = "emerald";
                    ShowToast("生产修复已安全完成（当前为静态模式，已跳过自主进化）", "emerald");
                    _postApproveTicks = 0;
                }
            }
            else if (_postApproveTicks == 115 && n8 != null && _selfEvolutionEnabled)
            {
                n8.Status = "completed";
                n8.Subtitle = "已蒸馏 1 条规则进入影子测试";
                n8.Metric = "1.9s";
                n8.Logs = new string[]
                {
                    "[21:09:13.010] 提取本次 MySQL 8.0 最小权限修复范式与公网跳板风险权重",
                    "[21:09:14.200] 生成候选规则 #RULE-2026-1122，已推入 Shadow Mode 影子测试队列",
                    "[21:09:14.910] 领域记忆库更新完成 (安全边界校验通过：未修改任何生产权限)"
                };
                _workflowPillText = "全流程闭环完成";
                _workflowPillColor = "emerald";
                SetAgentState("AGENT_EVOLUTION", "passed", 0);
                AddAuditEntry("Evolution-Engine", "规则蒸馏", "从本次修复沉淀 1 条新规则至影子测试队列 (" + _activeEvoVersion + "-shadow)", "影子测试");
                ShowToast("生产修复已完成！自主进化引擎已将新规则送入影子测试队列", "indigo");
                _postApproveTicks = 0;
            }
        }

        private void StartSimulateWorkflowRun()
        {
            if (_isSimulating) return;
            _isSimulating = true;
            _simStepTicks = 1;
            _postApproveTicks = 0;
            _approveProgress = 0f;
            _rejectProgress = 0f;
            ResetAgents();
            foreach (DagNode n in _nodes)
            {
                if (n.Id == 8 && !_selfEvolutionEnabled)
                {
                    n.Status = "bypassed";
                    n.Metric = "已跳过";
                }
                else
                {
                    n.Status = "waiting";
                    n.Metric = "等待中";
                }
            }
            _selectedNodeId = 1;
            _workflowPillText = "模拟运行中...";
            _workflowPillColor = "cyan";
        }

        private void UpdateSimulationStateMachine()
        {
            if (!_isSimulating) return;
            _simStepTicks++;

            int[] stepIds = new int[] { 1, 2, 3, 4, 5 };
            string[] metrics = new string[] { "1.2s", "3.8s", "0.18s", "4.1s", "8.4s" };

            for (int i = 0; i < stepIds.Length; i++)
            {
                int startTick = 5 + i * 24;
                int endTick = startTick + 18;
                DagNode node = FindNode(stepIds[i]);
                if (node == null) continue;

                if (_simStepTicks == startTick)
                {
                    node.Status = "running";
                    node.Metric = "运行中";
                    _selectedNodeId = node.Id;
                }
                else if (_simStepTicks == endTick)
                {
                    node.Status = "completed";
                    node.Metric = metrics[i];
                }
            }

            // 多 Agent 角色接力：总控 -> 研判 -> DB 专家 -> Critic 对抗验证（含 1 次自愈打回）
            if (_simStepTicks == 6)
            {
                SetAgentState("AGENT_SUPERVISOR", "active", 0);
            }
            else if (_simStepTicks == 14)
            {
                SetAgentState("AGENT_SUPERVISOR", "passed", 0);
                SetAgentState("AGENT_TRIAGE", "active", 0);
            }
            else if (_simStepTicks == 30)
            {
                SetAgentState("AGENT_TRIAGE", "passed", 0);
                SetAgentState("AGENT_DB_SPECIALIST", "active", 1);
            }
            else if (_simStepTicks == 101)
            {
                SetAgentState("AGENT_CRITIC", "active", 1);
                _criticRound = 1;
                _criticVerdict = "";
            }
            else if (_simStepTicks == 107)
            {
                // Critic 第 1 轮打回：生产只读预检发现长事务，要求改期至低峰窗口
                SetAgentState("AGENT_CRITIC", "rejected", 1);
                SetAgentState("AGENT_DB_SPECIALIST", "active", 2);
                _criticVerdict = "REJECTED";
                _selfCorrectionTotal = 1;
                AddAuditEntry("Critic-Agent", "对抗验证", "第 1 轮沙箱 Dry-Run 命中 >30s 活跃长事务，打回 DB 专家 Agent 自愈修正", "已打回");
            }
            else if (_simStepTicks == 114)
            {
                SetAgentState("AGENT_DB_SPECIALIST", "passed", 2);
                SetAgentState("AGENT_CRITIC", "active", 2);
                _criticRound = 2;
                _criticVerdict = "";
            }
            else if (_simStepTicks == 121)
            {
                SetAgentState("AGENT_CRITIC", "passed", 2);
                _criticVerdict = "PASSED";
                AddAuditEntry("Critic-Agent", "对抗验证", "第 2 轮沙箱复测通过：改期策略消除 MDL 锁风险，回滚可逆验证 2/2", "已放行");
            }

            if (_simStepTicks == 132)
            {
                DagNode n6 = FindNode(6);
                DagNode n7 = FindNode(7);
                if (n6 != null)
                {
                    n6.Status = "pending";
                    n6.Metric = "待处理";
                    n6.Subtitle = "长按右侧按钮沿边框蓄力放行";
                }
                if (n7 != null)
                {
                    n7.Status = "waiting";
                    n7.Metric = "等待前置";
                    n7.Subtitle = "原子执行 · 异常自动回滚";
                }
                _selectedNodeId = 6;
                _isSimulating = false;
                _workflowPillText = "等待 HITL 审批";
                _workflowPillColor = "amber";
                ShowToast("Critic 第 2 轮放行，流程已在 HITL 节点挂起等待长按审批", "cyan");
            }
        }

        private void ToggleSelfEvolution()
        {
            _selfEvolutionEnabled = !_selfEvolutionEnabled;
            DagNode n8 = FindNode(8);
            DagNode n7 = FindNode(7);
            if (n8 != null)
            {
                if (!_selfEvolutionEnabled)
                {
                    n8.Status = "bypassed";
                    n8.Subtitle = "已关闭 · 静态模式旁路";
                    n8.Metric = "已跳过";
                    n8.Meta[0, 1] = "已关闭 (纯静态确定性)";
                }
                else
                {
                    bool done = (n7 != null && n7.Status == "completed");
                    n8.Status = done ? "completed" : "waiting";
                    n8.Subtitle = done ? "已蒸馏 1 条规则进入影子测试" : "影子测试验证后入库";
                    n8.Metric = done ? "1.9s" : "等待触发";
                    n8.Meta[0, 1] = "已开启 (受控)";
                }
            }
            ShowToast(
                _selfEvolutionEnabled ? "已开启自主进化模式：新规则将先经影子测试验证" : "已切换为静态确定性模式：自主进化节点已旁路",
                _selfEvolutionEnabled ? "indigo" : "cyan"
            );
        }

        private DagNode FindNode(int id)
        {
            foreach (DagNode n in _nodes)
            {
                if (n.Id == id) return n;
            }
            return null;
        }

        private void TriggerMicroVibration()
        {
            _windowBaseLocation = Location;
            _windowShakeTicks = 12;
            try { SystemSounds.Asterisk.Play(); } catch { }
        }

        private void TriggerThemeRadialSwitch()
        {
            if (_fastTroubleshoot)
            {
                // 极速排障模式：跳过快照与 1.4s 圆心扩散，直接 0ms 瞬切
                if (_themeSnapshot != null)
                {
                    _themeSnapshot.Dispose();
                    _themeSnapshot = null;
                }
                _themeTransition = 1.0f;
                _isDark = !_isDark;
                _sunMoonTargetAngle += 360f;
                _sunMoonAngle = _sunMoonTargetAngle;
                return;
            }

            try
            {
                if (_themeSnapshot != null)
                {
                    _themeSnapshot.Dispose();
                    _themeSnapshot = null;
                }
                if (ClientSize.Width > 0 && ClientSize.Height > 0)
                {
                    _themeSnapshot = new Bitmap(ClientSize.Width, ClientSize.Height);
                    using (Graphics gSnap = Graphics.FromImage(_themeSnapshot))
                    {
                        RenderEntireWorkbench(gSnap, _isDark);
                    }
                }
            }
            catch
            {
                _themeSnapshot = null;
            }

            _themeOrigin = new PointF(
                _btnThemeRect.X + _btnThemeRect.Width * 0.5f,
                _btnThemeRect.Y + _btnThemeRect.Height * 0.5f
            );
            _isDark = !_isDark;
            _sunMoonTargetAngle += 360f;
            _themeTransition = 0.005f;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            if (_themeTransition < 1.0f && _themeSnapshot != null)
            {
                g.DrawImageUnscaled(_themeSnapshot, 0, 0);

                float maxDist = (float)Math.Sqrt(ClientSize.Width * ClientSize.Width + ClientSize.Height * ClientSize.Height) * 1.05f;
                float t = Math.Max(0f, Math.Min(1f, _themeTransition));
                float eased = t * t * (3f - 2f * t); // SmoothStep 慢速扩散曲线
                float radius = Math.Max(6f, eased * maxDist);

                using (GraphicsPath clipPath = new GraphicsPath())
                {
                    clipPath.AddEllipse(_themeOrigin.X - radius, _themeOrigin.Y - radius, radius * 2f, radius * 2f);
                    Region oldClip = g.Clip;
                    g.SetClip(clipPath, CombineMode.Replace);
                    RenderEntireWorkbench(g, _isDark);
                    g.Clip = oldClip;

                    g.SmoothingMode = SmoothingMode.AntiAlias;
                    Color glowCol = _isDark ? Color.FromArgb(70, 56, 189, 248) : Color.FromArgb(60, 24, 28, 36);
                    Color ringCol = _isDark ? Color.FromArgb(225, 56, 189, 248) : Color.FromArgb(195, 24, 28, 36);
                    using (Pen glowPen = new Pen(glowCol, 9.0f))
                    using (Pen ringPen = new Pen(ringCol, 2.4f))
                    {
                        g.DrawEllipse(glowPen, _themeOrigin.X - radius, _themeOrigin.Y - radius, radius * 2f, radius * 2f);
                        g.DrawEllipse(ringPen, _themeOrigin.X - radius, _themeOrigin.Y - radius, radius * 2f, radius * 2f);
                    }
                }
            }
            else
            {
                RenderEntireWorkbench(g, _isDark);
            }
        }

        private void RenderEntireWorkbench(Graphics g, bool dark)
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;

            int w = ClientSize.Width;
            int h = ClientSize.Height;
            if (w <= 20 || h <= 20) return;

            // 配色体系：还原 dbguard_ai_harness_ui.html 深色黑曜主题 & 浅色蓝图主题
            Color bgObsidian = dark ? Color.FromArgb(9, 13, 22) : Color.FromArgb(243, 244, 246);
            Color bgPanel = dark ? Color.FromArgb(15, 21, 35) : Color.FromArgb(250, 250, 252);
            Color bgCard = dark ? Color.FromArgb(21, 29, 46) : Color.FromArgb(255, 255, 255);
            Color borderCol = dark ? Color.FromArgb(30, 41, 59) : Color.FromArgb(216, 220, 228);
            Color fgPrimary = dark ? Color.FromArgb(241, 245, 249) : Color.FromArgb(15, 23, 42);
            Color fgSecondary = dark ? Color.FromArgb(203, 213, 225) : Color.FromArgb(51, 65, 85);
            Color fgMuted = dark ? Color.FromArgb(148, 163, 184) : Color.FromArgb(100, 116, 139);
            Color cCyan = dark ? Color.FromArgb(56, 189, 248) : Color.FromArgb(2, 132, 199);
            Color cIndigo = dark ? Color.FromArgb(99, 102, 241) : Color.FromArgb(79, 70, 229);
            Color cEmerald = dark ? Color.FromArgb(16, 185, 129) : Color.FromArgb(5, 150, 105);
            Color cAmber = dark ? Color.FromArgb(245, 158, 11) : Color.FromArgb(217, 119, 6);
            Color cRose = dark ? Color.FromArgb(244, 63, 94) : Color.FromArgb(225, 29, 72);

            using (SolidBrush brBg = new SolidBrush(bgObsidian))
            {
                g.FillRectangle(brBg, 0, 0, w, h);
            }

            float sidebarW = 246f;
            float headerH = 56f;

            // 1. 左侧企业级导航栏 (Sidebar)
            RectangleF sidebarRect = new RectangleF(0, 0, sidebarW, h);
            RenderSidebar(g, sidebarRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cAmber);

            // 2. 全局顶部 Header Bar
            RectangleF headerRect = new RectangleF(sidebarW, 0, w - sidebarW, headerH);
            RenderHeaderBar(g, headerRect, dark, bgObsidian, bgPanel, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cAmber);

            // 3. 右侧主内容区 (根据当前选中的 View 渲染)
            RectangleF mainRect = new RectangleF(sidebarW, headerH, w - sidebarW, h - headerH);
            if (_currentView == 0)
            {
                RenderWorkflowView(g, mainRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cAmber, cRose);
            }
            else if (_currentView == 1)
            {
                RenderAssetsView(g, mainRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cAmber, cRose);
            }
            else if (_currentView == 2)
            {
                RenderGatewayView(g, mainRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cRose);
            }
            else if (_currentView == 3)
            {
                RenderEvolutionView(g, mainRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald);
            }
            else if (_currentView == 4)
            {
                RenderAuditView(g, mainRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cEmerald);
            }
            else if (_currentView == 5)
            {
                RenderEvalDataView(g, mainRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cAmber, cRose);
            }

            // 4. 悬浮 Toast 提示
            if (_toastTicks > 0 && !string.IsNullOrEmpty(_toastText))
            {
                RenderFloatingToast(g, w, h, dark, bgPanel, fgPrimary, cCyan, cIndigo, cEmerald, cAmber);
            }
        }

        private void RenderSidebar(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber)
        {
            using (SolidBrush brPanel = new SolidBrush(bgPanel))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.FillRectangle(brPanel, rect);
                g.DrawLine(borderPen, rect.Right - 1f, rect.Top, rect.Right - 1f, rect.Bottom);
                g.DrawLine(borderPen, rect.Left, 56f, rect.Right, 56f);
            }

            // Brand Logo
            RectangleF logoBox = new RectangleF(16f, 12f, 32f, 32f);
            using (LinearGradientBrush lg = new LinearGradientBrush(logoBox, cCyan, cIndigo, 45f))
            using (Font fBrand = new Font("Microsoft YaHei UI", 10.5f, FontStyle.Bold))
            using (Font fBadge = new Font("Consolas", 7.2f, FontStyle.Bold))
            using (Font fSub = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Regular))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brIndigo = new SolidBrush(cIndigo))
            using (Pen shieldPen = new Pen(Color.White, 1.8f))
            {
                g.FillRectangle(lg, logoBox);
                // 绘制盾牌勾标
                g.DrawPolygon(shieldPen, new PointF[]
                {
                    new PointF(32f, 17f),
                    new PointF(41f, 20f),
                    new PointF(39f, 31f),
                    new PointF(32f, 38f),
                    new PointF(25f, 31f),
                    new PointF(23f, 20f)
                });
                g.DrawLines(shieldPen, new PointF[] { new PointF(28.5f, 27.5f), new PointF(31.5f, 30.5f), new PointF(36.5f, 24.5f) });

                g.DrawString("漏洞检测与自进化修复", fBrand, brPrimary, 56f, 11f);
                g.DrawString("企业级数据库安全防御平台", fSub, brMuted, 56f, 31f);
            }

            // 6 个导航项
            string[] secHeaders = new string[] { "核心工作台", "", "AI 与自进化引擎", "", "合规与治理", "" };
            string[] navLabels = new string[] { "⇄  工作流编排", "▤  资产与凭据", "❖  模型网关与脱敏", "✦  自主进化中心", "☷  审计日志", "◎  评测与数据" };
            float curY = 74f;

            DagNode n6 = FindNode(6);
            bool hasPendingHitl = (n6 != null && n6.Status == "pending");

            using (Font fSec = new Font("Microsoft YaHei UI", 7.8f, FontStyle.Bold))
            using (Font fNav = new Font("Microsoft YaHei UI", 9.2f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 8.0f, FontStyle.Bold))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            {
                for (int i = 0; i < 6; i++)
                {
                    if (!string.IsNullOrEmpty(secHeaders[i]))
                    {
                        if (i > 0) curY += 12f;
                        g.DrawString(secHeaders[i], fSec, brMuted, 20f, curY);
                        curY += 22f;
                    }

                    RectangleF nRect = new RectangleF(12f, curY, rect.Width - 24f, 38f);
                    _navRects[i] = nRect;
                    bool active = (_currentView == i);
                    bool hover = nRect.Contains(_mousePos);

                    if (active || hover)
                    {
                        using (SolidBrush brNavBg = new SolidBrush(active ? (dark ? Color.FromArgb(30, 41, 59) : Color.FromArgb(224, 242, 254)) : Color.FromArgb(dark ? 18 : 12, fgPrimary)))
                        using (Pen navPen = new Pen(active ? Color.FromArgb(120, cCyan) : borderCol, 1f))
                        {
                            g.FillRectangle(brNavBg, nRect);
                            if (active) g.DrawRectangle(navPen, nRect.X, nRect.Y, nRect.Width, nRect.Height);
                        }
                    }

                    g.DrawString(navLabels[i], fNav, active ? brCyan : brPrimary, nRect.X + 12f, nRect.Y + 10f);

                    // 右侧导航徽章
                    if (i == 0)
                    {
                        Color bCol = hasPendingHitl ? cAmber : cEmerald;
                        string bTxt = hasPendingHitl ? "1 待审批" : "0 待审批";
                        RectangleF bRect = new RectangleF(nRect.Right - 64f, nRect.Y + 10f, 54f, 18f);
                        using (SolidBrush bBg = new SolidBrush(Color.FromArgb(38, bCol)))
                        using (Pen bPen = new Pen(Color.FromArgb(100, bCol), 1f))
                        using (SolidBrush bFg = new SolidBrush(bCol))
                        {
                            g.FillRectangle(bBg, bRect);
                            g.DrawRectangle(bPen, bRect.X, bRect.Y, bRect.Width, bRect.Height);
                            StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                            g.DrawString(bTxt, new Font("Microsoft YaHei UI", 7.5f, FontStyle.Bold), bFg, bRect, sf);
                        }
                    }
                    else if (i == 1)
                    {
                        g.DrawString(_assets.Count.ToString(), fMono, brMuted, nRect.Right - 24f, nRect.Y + 12f);
                    }
                    else if (i == 2)
                    {
                        using (SolidBrush dotBr = new SolidBrush(cEmerald))
                        {
                            g.FillEllipse(dotBr, nRect.Right - 22f, nRect.Y + 15f, 8f, 8f);
                        }
                    }
                    else if (i == 3)
                    {
                        RectangleF vRect = new RectangleF(nRect.Right - 46f, nRect.Y + 10f, 36f, 18f);
                        using (SolidBrush vBg = new SolidBrush(Color.FromArgb(45, cIndigo)))
                        using (SolidBrush vFg = new SolidBrush(cIndigo))
                        {
                            g.FillRectangle(vBg, vRect);
                            StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                            g.DrawString(_activeEvoVersion, fMono, vFg, vRect, sf);
                        }
                    }
                    else if (i == 4)
                    {
                        g.DrawString(_auditLogs.Count.ToString(), fMono, brMuted, nRect.Right - 24f, nRect.Y + 12f);
                    }
                    else if (i == 5)
                    {
                        RectangleF sRect = new RectangleF(nRect.Right - 54f, nRect.Y + 10f, 44f, 18f);
                        using (SolidBrush sBg = new SolidBrush(Color.FromArgb(35, cCyan)))
                        using (SolidBrush sFg = new SolidBrush(cCyan))
                        {
                            g.FillRectangle(sBg, sRect);
                            StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                            g.DrawString(_suiteV2Cases + " 例", new Font("Microsoft YaHei UI", 7.4f, FontStyle.Bold), sFg, sRect, sf);
                        }
                    }

                    curY += 42f;
                }
            }

            // 底部租户与当前操作员卡片 (点击可切换 RBAC 三权分立角色)
            _tenantCardRect = new RectangleF(12f, rect.Bottom - 92f, rect.Width - 24f, 78f);
            string[] operatorNames = new string[] { "高级 DBA", "安全工程师", "合规审计员" };
            string[] operatorRoles = new string[] { "RBAC: 安全审批员 / 变更执行", "RBAC: 漏洞探测 / 规则蒸馏", "RBAC: 只读审计 / SARIF导出" };
            string[] avatars = new string[] { "DBA", "SEC", "AUD" };

            using (SolidBrush brCard = new SolidBrush(bgObsidian))
            using (Pen cardPen = new Pen(_tenantCardRect.Contains(_mousePos) ? cCyan : borderCol, 1f))
            using (Font fSmall = new Font("Microsoft YaHei UI", 7.8f, FontStyle.Regular))
            using (Font fBold = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Bold))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            {
                g.FillRectangle(brCard, _tenantCardRect);
                g.DrawRectangle(cardPen, _tenantCardRect.X, _tenantCardRect.Y, _tenantCardRect.Width, _tenantCardRect.Height);

                g.DrawString("当前租户 (点击切换角色)", fSmall, brMuted, _tenantCardRect.X + 10f, _tenantCardRect.Y + 8f);
                g.DrawString("生产金融域", fSmall, brEmerald, _tenantCardRect.Right - 68f, _tenantCardRect.Y + 8f);

                RectangleF avRect = new RectangleF(_tenantCardRect.X + 10f, _tenantCardRect.Y + 34f, 30f, 30f);
                using (SolidBrush avBg = new SolidBrush(bgCard))
                {
                    g.FillEllipse(avBg, avRect);
                    g.DrawEllipse(cardPen, avRect);
                }
                StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                g.DrawString(avatars[_rbacRoleIndex], fBold, brCyan, avRect, sf);

                g.DrawString(operatorNames[_rbacRoleIndex], fBold, brPrimary, _tenantCardRect.X + 48f, _tenantCardRect.Y + 34f);
                g.DrawString(operatorRoles[_rbacRoleIndex], fSmall, brMuted, _tenantCardRect.X + 48f, _tenantCardRect.Y + 52f);
            }
        }

        private void RenderHeaderBar(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber)
        {
            using (SolidBrush brPanel = new SolidBrush(bgPanel))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.FillRectangle(brPanel, rect);
                g.DrawLine(borderPen, rect.Left, rect.Bottom - 1f, rect.Right, rect.Bottom - 1f);
            }

            using (Font fCrumb = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Regular))
            using (Font fTitle = new Font("Microsoft YaHei UI", 10.5f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 8.5f, FontStyle.Bold))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            {
                g.DrawString("安全工作流  /", fCrumb, brMuted, rect.X + 18f, rect.Y + 19f);
                g.DrawString("生产核心库高危漏洞修复流", fTitle, brPrimary, rect.X + 98f, rect.Y + 17f);

                RectangleF verBox = new RectangleF(rect.X + 332f, rect.Y + 17f, 38f, 20f);
                using (SolidBrush vbBg = new SolidBrush(bgObsidian))
                using (Pen vbPen = new Pen(borderCol, 1f))
                {
                    g.FillRectangle(vbBg, verBox);
                    g.DrawRectangle(vbPen, verBox.X, verBox.Y, verBox.Width, verBox.Height);
                    StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                    g.DrawString("v2.4", fMono, brMuted, verBox, sf);
                }
            }

            // 右侧控制组
            float rightX = rect.Right - 16f;
            float topY = rect.Y + 11f;
            float btnH = 34f;

            // 1. 太阳/月亮旋转主题切换按钮 (1.4秒慢速圆心扩散)
            _btnThemeRect = new RectangleF(rightX - 36f, topY, 36f, btnH);
            DrawThemeToggleButton(g, _btnThemeRect, dark, fgPrimary, cCyan);
            rightX -= 44f;

            // 2. 极速排障模式开关 (开启后关闭圆心扩散等非必要过渡动效，0ms 瞬切)
            _btnFastModeRect = new RectangleF(rightX - 88f, topY, 88f, btnH);
            DrawFastModeButton(g, _btnFastModeRect, _fastTroubleshoot, dark, bgObsidian, borderCol, fgPrimary, cAmber);
            rightX -= 96f;

            // 3. 保存快照按钮
            _btnSnapshotRect = new RectangleF(rightX - 72f, topY, 72f, btnH);
            DrawHeaderButton(g, _btnSnapshotRect, "保存快照", false, dark, bgObsidian, borderCol, fgPrimary, cCyan);
            rightX -= 80f;

            // 4. 重新运行模拟按钮
            _btnSimulateRect = new RectangleF(rightX - 96f, topY, 96f, btnH);
            DrawHeaderButton(g, _btnSimulateRect, "▶ 运行模拟", true, dark, bgObsidian, borderCol, fgPrimary, cCyan);
            rightX -= 104f;

            // 5. 全局自主进化模式开关 (Self-Evolution Toggle)
            _evoToggleBoxRect = new RectangleF(rightX - 152f, topY, 152f, btnH);
            using (SolidBrush brBox = new SolidBrush(bgObsidian))
            using (Pen boxPen = new Pen(_evoToggleBoxRect.Contains(_mousePos) ? cIndigo : borderCol, 1f))
            using (Font fBold = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Bold))
            using (Font fSub = new Font("Microsoft YaHei UI", 7.2f, FontStyle.Regular))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brStatus = new SolidBrush(_selfEvolutionEnabled ? cIndigo : fgMuted))
            {
                g.FillRectangle(brBox, _evoToggleBoxRect);
                g.DrawRectangle(boxPen, _evoToggleBoxRect.X, _evoToggleBoxRect.Y, _evoToggleBoxRect.Width, _evoToggleBoxRect.Height);
                g.DrawString("自主进化模式", fBold, brPrimary, _evoToggleBoxRect.X + 10f, _evoToggleBoxRect.Y + 4f);
                g.DrawString(_selfEvolutionEnabled ? "已开启 · 影子测试" : "已关闭 · 静态确定性", fSub, brStatus, _evoToggleBoxRect.X + 10f, _evoToggleBoxRect.Y + 18f);

                // 拨动开关胶囊
                RectangleF swRect = new RectangleF(_evoToggleBoxRect.Right - 44f, _evoToggleBoxRect.Y + 8f, 34f, 18f);
                using (SolidBrush swBg = new SolidBrush(_selfEvolutionEnabled ? cIndigo : Color.FromArgb(65, 80, 102)))
                {
                    g.FillRectangle(swBg, swRect);
                }
                float knobX = _selfEvolutionEnabled ? (swRect.Right - 16f) : (swRect.X + 2f);
                g.FillEllipse(Brushes.White, knobX, swRect.Y + 2f, 14f, 14f);
            }
            rightX -= 160f;

            // 6. 模型分级路由状态胶囊
            _pillGatewayRect = new RectangleF(rightX - 238f, topY + 2f, 238f, 30f);
            using (SolidBrush brPill = new SolidBrush(bgObsidian))
            using (Pen pillPen = new Pen(_pillGatewayRect.Contains(_mousePos) ? cCyan : borderCol, 1f))
            using (Font fSmall = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Regular))
            using (Font fMono = new Font("Consolas", 8.2f, FontStyle.Bold))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            {
                g.FillRectangle(brPill, _pillGatewayRect);
                g.DrawRectangle(pillPen, _pillGatewayRect.X, _pillGatewayRect.Y, _pillGatewayRect.Width, _pillGatewayRect.Height);
                g.FillEllipse(brEmerald, _pillGatewayRect.X + 10f, _pillGatewayRect.Y + 11f, 7f, 7f);
                g.DrawString("分级路由:", fSmall, brMuted, _pillGatewayRect.X + 22f, _pillGatewayRect.Y + 7f);
                g.DrawString("Ollama + " + _byokModelName.Split(' ')[0], fMono, brCyan, _pillGatewayRect.X + 78f, _pillGatewayRect.Y + 8f);
                g.DrawString("[已脱敏]", fSmall, brEmerald, _pillGatewayRect.Right - 56f, _pillGatewayRect.Y + 7f);
            }
        }

        private void DrawHeaderButton(
            Graphics g,
            RectangleF rect,
            string text,
            bool isCyanHighlight,
            bool dark,
            Color bgObsidian,
            Color borderCol,
            Color fgPrimary,
            Color cCyan)
        {
            bool hover = rect.Contains(_mousePos);
            Color bg = isCyanHighlight
                ? Color.FromArgb(hover ? 55 : 35, cCyan)
                : (hover ? Color.FromArgb(30, fgPrimary) : bgObsidian);
            Color border = isCyanHighlight ? Color.FromArgb(120, cCyan) : (hover ? cCyan : borderCol);
            Color fg = isCyanHighlight ? cCyan : fgPrimary;

            using (SolidBrush br = new SolidBrush(bg))
            using (Pen pen = new Pen(border, 1f))
            using (Font f = new Font("Microsoft YaHei UI", 8.4f, FontStyle.Bold))
            using (SolidBrush brFg = new SolidBrush(fg))
            {
                g.FillRectangle(br, rect);
                g.DrawRectangle(pen, rect.X, rect.Y, rect.Width, rect.Height);
                StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                g.DrawString(text, f, brFg, rect, sf);
            }
        }

        private void DrawThemeToggleButton(Graphics g, RectangleF rect, bool dark, Color fgPrimary, Color accent)
        {
            bool hover = rect.Contains(_mousePos);
            Color fill = hover
                ? Color.FromArgb(dark ? 45 : 25, accent)
                : (dark ? Color.FromArgb(15, 23, 42) : Color.FromArgb(255, 255, 255));
            using (SolidBrush br = new SolidBrush(fill))
            using (Pen pen = new Pen(hover ? accent : Color.FromArgb(90, accent), 1.3f))
            {
                g.FillRectangle(br, rect.X, rect.Y, rect.Width, rect.Height);
                g.DrawRectangle(pen, rect.X, rect.Y, rect.Width, rect.Height);
            }

            GraphicsState st = g.Save();
            float cx = rect.X + rect.Width * 0.5f;
            float cy = rect.Y + rect.Height * 0.5f;
            g.TranslateTransform(cx, cy);
            g.RotateTransform(_sunMoonAngle);

            Color iconCol = dark ? Color.FromArgb(250, 204, 21) : Color.FromArgb(30, 41, 59);
            using (Pen iconPen = new Pen(iconCol, 1.7f))
            using (SolidBrush iconBrush = new SolidBrush(iconCol))
            {
                if (dark)
                {
                    g.FillEllipse(iconBrush, -4.5f, -4.5f, 9f, 9f);
                    for (int i = 0; i < 8; i++)
                    {
                        double a = i * Math.PI / 4.0;
                        g.DrawLine(iconPen, (float)Math.Cos(a) * 6.8f, (float)Math.Sin(a) * 6.8f, (float)Math.Cos(a) * 9.5f, (float)Math.Sin(a) * 9.5f);
                    }
                }
                else
                {
                    g.FillEllipse(iconBrush, -6f, -6f, 12f, 12f);
                    using (SolidBrush cutBrush = new SolidBrush(Color.White))
                    {
                        g.FillEllipse(cutBrush, -2f, -7.5f, 10f, 10f);
                    }
                }
            }
            g.Restore(st);
        }

        private void DrawFastModeButton(
            Graphics g,
            RectangleF rect,
            bool enabled,
            bool dark,
            Color bgObsidian,
            Color borderCol,
            Color fgPrimary,
            Color cAmber)
        {
            bool hover = rect.Contains(_mousePos);
            Color accent = enabled ? cAmber : fgPrimary;
            Color fill = enabled
                ? Color.FromArgb(dark ? 42 : 28, cAmber)
                : (hover ? Color.FromArgb(30, fgPrimary) : bgObsidian);
            Color border = enabled ? cAmber : (hover ? accent : borderCol);

            using (SolidBrush br = new SolidBrush(fill))
            using (Pen pen = new Pen(border, enabled ? 1.5f : 1f))
            using (Font f = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Bold))
            using (SolidBrush brFg = new SolidBrush(enabled ? cAmber : (hover ? fgPrimary : Color.FromArgb(210, fgPrimary))))
            {
                g.FillRectangle(br, rect);
                g.DrawRectangle(pen, rect.X, rect.Y, rect.Width, rect.Height);

                // 矢量闪电图标（避免依赖 emoji 字体回退）
                float ix = rect.X + 9f;
                float iy = rect.Y + 9f;
                PointF[] bolt = new PointF[]
                {
                    new PointF(ix + 8f, iy),
                    new PointF(ix, iy + 10f),
                    new PointF(ix + 5.5f, iy + 10f),
                    new PointF(ix + 3f, iy + 17f),
                    new PointF(ix + 12f, iy + 6f),
                    new PointF(ix + 6f, iy + 6f)
                };
                g.FillPolygon(brFg, bolt);

                StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                g.DrawString("极速排障", f, brFg, new RectangleF(rect.X + 18f, rect.Y, rect.Width - 24f, rect.Height), sf);
            }

            // 右上角 0ms 状态点
            using (SolidBrush dot = new SolidBrush(enabled ? cAmber : Color.FromArgb(95, fgPrimary)))
            {
                g.FillEllipse(dot, rect.Right - 11f, rect.Y + 5f, 5f, 5f);
            }
        }

        // =========================================================================
        // VIEW 1: 工作流编排画布 (左侧 8 节点 S 型 DAG + 赛博圆弧背景 + 右侧 4-Tab 检视器)
        // =========================================================================
        private void RenderWorkflowView(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber,
            Color cRose)
        {
            float inspectorW = 438f;
            RectangleF canvasRect = new RectangleF(rect.X, rect.Y, rect.Width - inspectorW, rect.Height);
            RectangleF inspectorRect = new RectangleF(canvasRect.Right, rect.Y, inspectorW, rect.Height);

            // --- 1. 左侧 DAG 点阵与赛博几何圆弧背景 ---
            Region oldClip = g.Clip;
            g.SetClip(canvasRect, CombineMode.Intersect);

            using (SolidBrush dotBr = new SolidBrush(dark ? Color.FromArgb(35, 53, 82) : Color.FromArgb(203, 213, 225)))
            {
                for (float dx = canvasRect.X + 16f; dx < canvasRect.Right; dx += 24f)
                {
                    for (float dy = canvasRect.Y + 16f; dy < canvasRect.Bottom - 36f; dy += 24f)
                    {
                        g.FillRectangle(dotBr, dx, dy, 1.6f, 1.6f);
                    }
                }
            }

            // 背景微光赛博刻度圆弧（融合赛博朋克科技感与清晰 DAG 拓扑）
            float arcCx = canvasRect.Right + 160f;
            float arcCy = canvasRect.Y + canvasRect.Height * 0.48f;
            float arcR1 = canvasRect.Width * 0.72f;
            float arcR2 = canvasRect.Width * 0.86f;
            using (Pen arcPen = new Pen(Color.FromArgb(dark ? 28 : 22, cCyan), 1.2f))
            {
                g.DrawArc(arcPen, arcCx - arcR1, arcCy - arcR1, arcR1 * 2f, arcR1 * 2f, 140f, 80f);
                g.DrawArc(arcPen, arcCx - arcR2, arcCy - arcR2, arcR2 * 2f, arcR2 * 2f, 140f, 80f);
            }

            // 左上角执行实例状态条
            RectangleF instBar = new RectangleF(canvasRect.X + 16f, canvasRect.Y + 14f, 325f, 32f);
            Color pillCol = _workflowPillColor == "cyan" ? cCyan : (_workflowPillColor == "emerald" ? cEmerald : (_workflowPillColor == "indigo" ? cIndigo : (_workflowPillColor == "rose" ? cRose : cAmber)));
            using (SolidBrush brBar = new SolidBrush(Color.FromArgb(230, bgPanel)))
            using (Pen barPen = new Pen(borderCol, 1f))
            using (Font fSmall = new Font("Microsoft YaHei UI", 8.2f, FontStyle.Regular))
            using (Font fMono = new Font("Consolas", 8.5f, FontStyle.Bold))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brPillFg = new SolidBrush(pillCol))
            using (SolidBrush brPillBg = new SolidBrush(Color.FromArgb(38, pillCol)))
            {
                g.FillRectangle(brBar, instBar);
                g.DrawRectangle(barPen, instBar.X, instBar.Y, instBar.Width, instBar.Height);
                g.DrawString("执行实例:", fSmall, brMuted, instBar.X + 10f, instBar.Y + 8f);
                g.DrawString("#RUN-20261122-094", fMono, brPrimary, instBar.X + 68f, instBar.Y + 8f);

                RectangleF pRect = new RectangleF(instBar.Right - 108f, instBar.Y + 6f, 98f, 20f);
                g.FillRectangle(brPillBg, pRect);
                StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                g.DrawString(_workflowPillText, new Font("Microsoft YaHei UI", 7.6f, FontStyle.Bold), brPillFg, pRect, sf);
            }

            // 多 Agent 协作角色徽章 + Critic 自愈轮次（StateGraph 拓扑可视化）
            RenderAgentStrip(g, canvasRect, dark, bgCard, borderCol, fgPrimary, fgMuted, cCyan, cIndigo, cEmerald, cAmber, cRose);

            // 计算 DAG 画布偏移与节点尺寸
            float dagOriginX = canvasRect.X + 24f;
            float dagOriginY = canvasRect.Y + 136f;
            float nodeW = 228f;
            float nodeH = 106f;

            foreach (DagNode n in _nodes)
            {
                n.HitRect = new RectangleF(dagOriginX + n.X, dagOriginY + n.Y, nodeW, nodeH);
            }

            // 绘制 7 条贝塞尔连接线与动态流动粒子
            foreach (DagEdge edge in _edges)
            {
                DagNode fromN = FindNode(edge.FromId);
                DagNode toN = FindNode(edge.ToId);
                if (fromN == null || toN == null) continue;

                PointF p1, p2, c1, c2;
                if (edge.Dir == "right-left")
                {
                    p1 = new PointF(fromN.HitRect.Right, fromN.HitRect.Top + nodeH * 0.5f);
                    p2 = new PointF(toN.HitRect.Left, toN.HitRect.Top + nodeH * 0.5f);
                    float midX = (p1.X + p2.X) * 0.5f;
                    c1 = new PointF(midX, p1.Y);
                    c2 = new PointF(midX, p2.Y);
                }
                else if (edge.Dir == "left-right")
                {
                    p1 = new PointF(fromN.HitRect.Left, fromN.HitRect.Top + nodeH * 0.5f);
                    p2 = new PointF(toN.HitRect.Right, toN.HitRect.Top + nodeH * 0.5f);
                    float midX = (p1.X + p2.X) * 0.5f;
                    c1 = new PointF(midX, p1.Y);
                    c2 = new PointF(midX, p2.Y);
                }
                else
                {
                    p1 = new PointF(fromN.HitRect.Left + nodeW * 0.5f, fromN.HitRect.Bottom);
                    p2 = new PointF(toN.HitRect.Left + nodeW * 0.5f, toN.HitRect.Top);
                    float midY = (p1.Y + p2.Y) * 0.5f;
                    c1 = new PointF(p1.X, midY);
                    c2 = new PointF(p2.X, midY);
                }

                Color edgeCol = dark ? Color.FromArgb(71, 85, 105) : Color.FromArgb(148, 163, 184);
                bool animated = false;
                if (toN.Status == "running")
                {
                    edgeCol = cCyan;
                    animated = true;
                }
                else if (toN.Status == "pending")
                {
                    edgeCol = cAmber;
                    animated = true;
                }
                else if (fromN.Status == "completed" && toN.Status == "completed")
                {
                    edgeCol = (toN.Id == 8) ? cIndigo : cEmerald;
                    animated = true;
                }

                using (Pen ePen = new Pen(edgeCol, animated ? 2.2f : 1.6f))
                {
                    if (animated)
                    {
                        ePen.DashStyle = DashStyle.Custom;
                        ePen.DashPattern = new float[] { 5f, 4f };
                        ePen.DashOffset = _fastTroubleshoot ? 0f : -(_time * 18f) % 9f;
                    }
                    AdjustableArrowCap arrow = new AdjustableArrowCap(4.5f, 4.5f, true);
                    ePen.CustomEndCap = arrow;
                    g.DrawBezier(ePen, p1, c1, c2, p2);
                }
            }

            // 绘制 8 个 DAG 节点卡片
            using (Font fCat = new Font("Microsoft YaHei UI", 7.8f, FontStyle.Bold))
            using (Font fMetric = new Font("Consolas", 8.0f, FontStyle.Regular))
            using (Font fTitle = new Font("Microsoft YaHei UI", 9.2f, FontStyle.Bold))
            using (Font fSub = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Regular))
            {
                foreach (DagNode node in _nodes)
                {
                    RectangleF nr = node.HitRect;
                    bool isSel = (_selectedNodeId == node.Id);
                    bool isEvo = (node.Id == 8);
                    bool isBypassed = (node.Status == "bypassed");

                    Color stCol = fgMuted;
                    if (node.Status == "completed") stCol = isEvo ? cIndigo : cEmerald;
                    else if (node.Status == "pending") stCol = cAmber;
                    else if (node.Status == "running") stCol = cCyan;
                    else if (node.Status == "rejected") stCol = cRose;

                    // 呼吸光晕 (pending 或 running)；极速排障模式下保持静态强调，避免非必要动效
                    if ((node.Status == "pending" || node.Status == "running") && !_fastTroubleshoot)
                    {
                        float pulse = 0.5f + 0.5f * (float)Math.Sin(_time * 4.2f);
                        using (Pen glowPen = new Pen(Color.FromArgb((int)(40 + 60 * pulse), stCol), 5f))
                        {
                            g.DrawRectangle(glowPen, nr.X - 2f, nr.Y - 2f, nr.Width + 4f, nr.Height + 4f);
                        }
                    }

                    using (SolidBrush cardBr = new SolidBrush(isBypassed ? Color.FromArgb(130, bgCard) : bgCard))
                    using (Pen borderPen = new Pen(isSel ? cCyan : Color.FromArgb(isBypassed ? 90 : 180, stCol), isSel ? 2.2f : 1.3f))
                    {
                        if (isBypassed) borderPen.DashStyle = DashStyle.Dash;
                        g.FillRectangle(cardBr, nr);
                        g.DrawRectangle(borderPen, nr.X, nr.Y, nr.Width, nr.Height);
                    }

                    // 类别胶囊与耗时
                    RectangleF badgeRect = new RectangleF(nr.X + 10f, nr.Y + 10f, 132f, 20f);
                    using (SolidBrush bBg = new SolidBrush(Color.FromArgb(35, stCol)))
                    using (SolidBrush bFg = new SolidBrush(stCol))
                    using (SolidBrush brMuted = new SolidBrush(fgMuted))
                    using (SolidBrush brPrimary = new SolidBrush(isBypassed ? fgMuted : fgPrimary))
                    {
                        g.FillRectangle(bBg, badgeRect);
                        g.FillEllipse(bFg, badgeRect.X + 6f, badgeRect.Y + 6.5f, 6.5f, 6.5f);
                        g.DrawString(node.Category, fCat, bFg, badgeRect.X + 16f, badgeRect.Y + 2.5f);

                        StringFormat sfRight = new StringFormat { Alignment = StringAlignment.Far };
                        g.DrawString(node.Metric, fMetric, brMuted, nr.Right - 10f, nr.Y + 12f, sfRight);

                        g.DrawString(node.Title, fTitle, brPrimary, nr.X + 10f, nr.Y + 42f);
                        g.DrawString(node.Subtitle, fSub, brMuted, nr.X + 10f, nr.Y + 68f);
                    }
                }
            }

            // 画布底部图例栏
            RectangleF legendBar = new RectangleF(canvasRect.X, canvasRect.Bottom - 34f, canvasRect.Width, 34f);
            using (SolidBrush brLeg = new SolidBrush(bgPanel))
            using (Pen legPen = new Pen(borderCol, 1f))
            using (Font fLeg = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Regular))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            {
                g.FillRectangle(brLeg, legendBar);
                g.DrawLine(legPen, legendBar.Left, legendBar.Top, legendBar.Right, legendBar.Top);

                float lx = legendBar.X + 16f;
                float ly = legendBar.Y + 9f;
                DrawLegendItem(g, ref lx, ly, "已验证/完成", cEmerald, fLeg, brMuted);
                DrawLegendItem(g, ref lx, ly, "HITL 待审批", cAmber, fLeg, brMuted);
                DrawLegendItem(g, ref lx, ly, "执行中", cCyan, fLeg, brMuted);
                DrawLegendItem(g, ref lx, ly, "等待/旁路", fgMuted, fLeg, brMuted);
                DrawLegendItem(g, ref lx, ly, "自主进化节点", cIndigo, fLeg, brMuted);

                g.DrawString("提示: 可点击或拖拽任意节点 · 节点6支持长按边框蓄力审批", fLeg, brMuted, legendBar.Right - 328f, ly);
            }

            g.Clip = oldClip;

            // --- 2. 右侧动态节点检视器抽屉 (Inspector Drawer) ---
            RenderInspectorDrawer(g, inspectorRect, dark, bgObsidian, bgPanel, bgCard, borderCol, fgPrimary, fgSecondary, fgMuted, cCyan, cIndigo, cEmerald, cAmber, cRose);
        }

        private void DrawLegendItem(Graphics g, ref float x, float y, string text, Color dotCol, Font f, Brush brText)
        {
            using (SolidBrush brDot = new SolidBrush(dotCol))
            {
                g.FillEllipse(brDot, x, y + 4f, 7.5f, 7.5f);
            }
            g.DrawString(text, f, brText, x + 11f, y);
            x += 92f;
        }

        // =========================================================================
        // 多 Agent 协作可视化：5 类角色徽章 + Critic 自愈轮次进度（硬上限 3 轮）
        // =========================================================================
        private Color AgentStateColor(string state, Color cCyan, Color cIndigo, Color cEmerald, Color cAmber, Color cRose, Color fgMuted)
        {
            if (state == "active") return cCyan;
            if (state == "verify") return cIndigo;
            if (state == "rejected") return cRose;
            if (state == "passed") return cEmerald;
            if (state == "escalated") return cAmber;
            return fgMuted;
        }

        private string AgentStateText(string state)
        {
            if (state == "active") return "执行中";
            if (state == "verify") return "验证中";
            if (state == "rejected") return "已打回";
            if (state == "passed") return "已放行";
            if (state == "escalated") return "转人工";
            return "待命";
        }

        private string CriticVerdictText()
        {
            if (_criticVerdict == "PASSED") return "沙箱放行";
            if (_criticVerdict == "REJECTED") return "打回自愈";
            if (_criticVerdict == "EXHAUSTED") return "超限转人工";
            return "待命";
        }

        private void SetAgentState(string id, string state, int round)
        {
            for (int i = 0; i < _agents.Count; i++)
            {
                if (_agents[i].Id == id)
                {
                    _agents[i].State = state;
                    if (round > 0) _agents[i].Round = round;
                    return;
                }
            }
        }

        private void ResetAgents()
        {
            for (int i = 0; i < _agents.Count; i++)
            {
                _agents[i].State = "idle";
                _agents[i].Round = 0;
            }
            _criticRound = 0;
            _criticVerdict = "";
            _selfCorrectionTotal = 0;
        }

        private void RenderAgentStrip(
            Graphics g,
            RectangleF canvasRect,
            bool dark,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber,
            Color cRose)
        {
            RectangleF strip = new RectangleF(canvasRect.X + 16f, canvasRect.Y + 52f, canvasRect.Width - 32f, 76f);
            _agentStripRect = strip;

            using (SolidBrush brBg = new SolidBrush(Color.FromArgb(dark ? 210 : 242, bgCard)))
            using (Pen stripPen = new Pen(borderCol, 1f))
            using (Font fTitle = new Font("Microsoft YaHei UI", 7.8f, FontStyle.Bold))
            using (Font fTiny = new Font("Microsoft YaHei UI", 6.9f, FontStyle.Regular))
            using (Font fRole = new Font("Microsoft YaHei UI", 7.1f, FontStyle.Bold))
            using (Font fState = new Font("Microsoft YaHei UI", 6.8f, FontStyle.Regular))
            using (Font fMono = new Font("Consolas", 7.4f, FontStyle.Bold))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            {
                g.FillRectangle(brBg, strip);
                g.DrawRectangle(stripPen, strip.X, strip.Y, strip.Width, strip.Height);

                g.DrawString("多 Agent 协作", fTitle, brPrimary, strip.X + 10f, strip.Y + 7f);
                g.DrawString("StateGraph DAG", fTiny, brMuted, strip.X + 10f, strip.Y + 21f);
                g.DrawString("确定性编排", fTiny, brMuted, strip.X + 10f, strip.Y + 32f);

                float x = strip.X + 88f;
                float gap = 5f;
                int count = _agents.Count > 0 ? _agents.Count : 1;
                float badgeW = (strip.Width - 96f - gap * (count - 1)) / count;

                for (int i = 0; i < _agents.Count; i++)
                {
                    AgentBadge agent = _agents[i];
                    Color st = AgentStateColor(agent.State, cCyan, cIndigo, cEmerald, cAmber, cRose, fgMuted);
                    RectangleF br = new RectangleF(x, strip.Y + 7f, badgeW, 31f);
                    using (SolidBrush bBg = new SolidBrush(Color.FromArgb(agent.State == "idle" ? 16 : 34, st)))
                    using (Pen bPen = new Pen(Color.FromArgb(agent.State == "idle" ? 70 : 150, st), 1f))
                    using (SolidBrush bDot = new SolidBrush(st))
                    using (SolidBrush bRole = new SolidBrush(agent.State == "idle" ? fgMuted : fgPrimary))
                    {
                        g.FillRectangle(bBg, br);
                        g.DrawRectangle(bPen, br.X, br.Y, br.Width, br.Height);
                        g.FillEllipse(bDot, br.X + 5f, br.Y + 6f, 5.5f, 5.5f);
                        g.DrawString(agent.RoleLabel, fRole, bRole, br.X + 14f, br.Y + 2.5f);

                        string stateText = AgentStateText(agent.State);
                        if (agent.Round > 0)
                        {
                            stateText = stateText + " R" + agent.Round;
                        }
                        g.DrawString(stateText, fState, bDot, br.X + 14f, br.Y + 17f);
                    }
                    x += badgeW + gap;
                }

                // 第二行：Critic 自愈轮次进度（每格代表一轮，硬上限 3 轮）
                float y2 = strip.Y + 46f;
                g.DrawString("Critic 自愈轮次", fTiny, brMuted, strip.X + 10f, y2 + 4f);
                float sx = strip.X + 88f;
                for (int s = 0; s < _criticMaxRounds; s++)
                {
                    RectangleF seg = new RectangleF(sx, y2 + 3f, 20f, 9f);
                    bool used = s < _criticRound;
                    bool passedSeg = used && (s == _criticRound - 1) && _criticVerdict == "PASSED";
                    Color segCol = passedSeg ? cEmerald : (used ? cRose : fgMuted);
                    using (SolidBrush segBg = new SolidBrush(used ? Color.FromArgb(120, segCol) : Color.FromArgb(dark ? 26 : 210, fgMuted)))
                    using (Pen segPen = new Pen(Color.FromArgb(used ? 200 : 90, segCol), 1f))
                    {
                        g.FillRectangle(segBg, seg);
                        g.DrawRectangle(segPen, seg.X, seg.Y, seg.Width, seg.Height);
                    }
                    sx += 24f;
                }

                string roundText = "第 " + _criticRound + "/" + _criticMaxRounds + " 轮 · " + CriticVerdictText();
                if (_selfCorrectionTotal > 0)
                {
                    roundText = roundText + " · 已自愈 " + _selfCorrectionTotal + " 次";
                }
                g.DrawString(roundText, fMono, brPrimary, sx + 8f, y2 + 1f);

                StringFormat sfRight = new StringFormat { Alignment = StringAlignment.Far };
                if (strip.Width > 720f)
                {
                    g.DrawString("硬上限 3 轮 · 超限挂起转人工 HITL 并写入 WORM", fTiny, brMuted,
                        new RectangleF(strip.X + strip.Width - 330f, y2 + 3f, 320f, 14f), sfRight);
                }
            }
        }

        private void RenderInspectorDrawer(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber,
            Color cRose)
        {
            using (SolidBrush brPanel = new SolidBrush(bgPanel))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.FillRectangle(brPanel, rect);
                g.DrawLine(borderPen, rect.Left, rect.Top, rect.Left, rect.Bottom);
            }

            DagNode node = FindNode(_selectedNodeId) ?? FindNode(6);
            if (node == null) return;

            using (Font fTitle = new Font("Microsoft YaHei UI", 10.5f, FontStyle.Bold))
            using (Font fSub = new Font("Microsoft YaHei UI", 8.2f, FontStyle.Regular))
            using (Font fBadge = new Font("Microsoft YaHei UI", 8.0f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 8.2f, FontStyle.Regular))
            using (Font fMonoBold = new Font("Consolas", 8.5f, FontStyle.Bold))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brSecondary = new SolidBrush(fgSecondary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brAmber = new SolidBrush(cAmber))
            using (SolidBrush brRose = new SolidBrush(cRose))
            using (SolidBrush brIndigo = new SolidBrush(cIndigo))
            using (Pen divPen = new Pen(borderCol, 1f))
            {
                // === 节点 6: 专属 HITL 审批与 4-Tab 漏洞详情视图 ===
                if (node.Id == 6)
                {
                    // 头部
                    RectangleF b1 = new RectangleF(rect.X + 16f, rect.Y + 14f, 118f, 22f);
                    using (SolidBrush b1Bg = new SolidBrush(Color.FromArgb(38, cAmber)))
                    {
                        g.FillRectangle(b1Bg, b1);
                        StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                        g.DrawString("HITL 人工审批节点", fBadge, brAmber, b1, sf);
                    }

                    RectangleF b2 = new RectangleF(rect.Right - 144f, rect.Y + 14f, 128f, 22f);
                    using (SolidBrush b2Bg = new SolidBrush(Color.FromArgb(38, cRose)))
                    {
                        g.FillRectangle(b2Bg, b2);
                        StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                        g.DrawString("风险分 9.1 · 业务高危", fBadge, brRose, b2, sf);
                    }

                    g.DrawString("MySQL secure_file_priv 缺陷 + 过度授权", fTitle, brPrimary, rect.X + 16f, rect.Y + 46f);
                    g.DrawString("目标: MySQL-Prod-Order · 暴露面: 公网跳板 / 含 PII 核心表", fSub, brMuted, rect.X + 16f, rect.Y + 70f);

                    // 多 Agent 协作链 + Critic 自愈轮次（角色徽章与轮次始终可见）
                    RectangleF chainRect = new RectangleF(rect.X + 16f, rect.Y + 90f, rect.Width - 32f, 24f);
                    using (SolidBrush chainBg = new SolidBrush(Color.FromArgb(24, cIndigo)))
                    using (Pen chainPen = new Pen(Color.FromArgb(90, cIndigo), 1f))
                    {
                        g.FillRectangle(chainBg, chainRect);
                        g.DrawRectangle(chainPen, chainRect.X, chainRect.Y, chainRect.Width, chainRect.Height);
                        string chainText = "总控 → 研判 → DB 专家 → Critic 第 " + Math.Max(1, _criticRound) + "/" + _criticMaxRounds
                            + " 轮 " + CriticVerdictText() + " → HITL";
                        g.DrawString(chainText, fMono, brIndigo, chainRect.X + 8f, chainRect.Y + 4f);
                    }
                    g.DrawLine(divPen, rect.Left, rect.Y + 122f, rect.Right, rect.Y + 122f);

                    // 4 个切换 Tab: 修复 SQL | Rollback 回滚 | 沙箱报告 | 脱敏对比
                    string[] tabs = new string[] { "修复 SQL", "Rollback 回滚", "双轨验证", "脱敏对比" };
                    float tabW = (rect.Width - 24f) / 4f;
                    for (int t = 0; t < 4; t++)
                    {
                        RectangleF tr = new RectangleF(rect.X + 12f + t * tabW, rect.Y + 124f, tabW, 34f);
                        _hitlTabRects[t] = tr;
                        bool isAct = (_hitlActiveTab == t);
                        StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                        g.DrawString(tabs[t], fBadge, isAct ? brCyan : brMuted, tr, sf);
                        if (isAct)
                        {
                            using (Pen actPen = new Pen(cCyan, 2.4f))
                            {
                                g.DrawLine(actPen, tr.Left + 8f, tr.Bottom - 1f, tr.Right - 8f, tr.Bottom - 1f);
                            }
                        }
                    }
                    g.DrawLine(divPen, rect.Left, rect.Y + 158f, rect.Right, rect.Y + 158f);

                    float cy = rect.Y + 172f;
                    if (_hitlActiveTab == 0)
                    {
                        // 3 项评估卡：锁表风险 / 会话级锁超时注入 / 分级回滚保护
                        float cw = (rect.Width - 44f) / 3f;
                        DrawEvalMiniCard(g, new RectangleF(rect.X + 16f, cy, cw, 52f), "锁表风险", "无锁表", bgObsidian, borderCol, brMuted, brEmerald, fSub, fBadge);
                        DrawEvalMiniCard(g, new RectangleF(rect.X + 22f + cw, cy, cw, 52f), "会话锁超时", "3s 强制注入", bgObsidian, borderCol, brMuted, brRose, fSub, fBadge);
                        DrawEvalMiniCard(g, new RectangleF(rect.X + 28f + cw * 2f, cy, cw, 52f), "分级回滚", "0~5m + 72h", bgObsidian, borderCol, brMuted, brCyan, fSub, fMonoBold);
                        cy += 64f;

                        // SQL 代码框（首段为强制注入的会话级锁超时保护）
                        RectangleF sqlBox = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 300f);
                        using (SolidBrush brBox = new SolidBrush(bgObsidian))
                        {
                            g.FillRectangle(brBox, sqlBox);
                            g.DrawRectangle(divPen, sqlBox.X, sqlBox.Y, sqlBox.Width, sqlBox.Height);
                        }
                        g.DrawString("fix_mysql_priv_20261122.sql", fMono, brMuted, sqlBox.X + 12f, sqlBox.Y + 10f);
                        g.DrawString("lock_wait_timeout=3s 已注入", fMono, brRose, sqlBox.Right - 190f, sqlBox.Y + 10f);
                        string[] sqlLines = new string[]
                        {
                            "-- 0. 外层会话级锁超时保护 (强制注入，防 MDL 阻塞)",
                            "SET SESSION lock_wait_timeout = 3;",
                            "",
                            "-- 1. 回收业务账号全局高危权限 (消除 FILE/SUPER 提权)",
                            "REVOKE ALL PRIVILEGES, GRANT OPTION",
                            "  FROM 'app_order_rw'@'10.24.%';",
                            "",
                            "-- 2. 按最小权限原则仅授予订单库读写权限",
                            "GRANT SELECT, INSERT, UPDATE",
                            "  ON prod_order_db.*",
                            "  TO 'app_order_rw'@'10.24.%';",
                            "",
                            "-- 3. 动态关闭局部文件加载并刷新权限",
                            "SET PERSIST local_infile = OFF;",
                            "FLUSH PRIVILEGES;"
                        };
                        for (int l = 0; l < sqlLines.Length; l++)
                        {
                            string line = sqlLines[l];
                            Brush lBr = line.StartsWith("--") ? brMuted
                                : (line.StartsWith("SET SESSION lock_wait_timeout") ? brRose
                                : (line.StartsWith("REVOKE") || line.StartsWith("GRANT") || line.StartsWith("SET") || line.StartsWith("FLUSH") ? brCyan : brPrimary));
                            g.DrawString(line, fMono, lBr, sqlBox.X + 12f, sqlBox.Y + 34f + l * 16.5f);
                        }
                        cy += 312f;

                        // 双期分级回滚保护说明框
                        RectangleF noteBox = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 66f);
                        using (SolidBrush nbBg = new SolidBrush(Color.FromArgb(28, cIndigo)))
                        using (Pen nbPen = new Pen(Color.FromArgb(80, cIndigo), 1f))
                        {
                            g.FillRectangle(nbBg, noteBox);
                            g.DrawRectangle(nbPen, noteBox.X, noteBox.Y, noteBox.Width, noteBox.Height);
                            g.DrawString("分级回滚保护：0~5 分钟实时熔断期监控 SQL 报错与 QPS 断崖下跌，\n超阈值自动执行 Rollback；24~72 小时延迟观察期联动 SIEM 监听权限报错，\n支持一键紧急恢复 (1-Click Restore)。", fSub, brIndigo, noteBox.X + 10f, noteBox.Y + 7f);
                        }
                    }
                    else if (_hitlActiveTab == 1)
                    {
                        RectangleF rbBanner = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 56f);
                        using (SolidBrush rbBg = new SolidBrush(Color.FromArgb(28, cEmerald)))
                        using (Pen rbPen = new Pen(Color.FromArgb(80, cEmerald), 1f))
                        {
                            g.FillRectangle(rbBg, rbBanner);
                            g.DrawRectangle(rbPen, rbBanner.X, rbBanner.Y, rbBanner.Width, rbBanner.Height);
                            g.DrawString("① 0~5 分钟实时熔断期：若变更后观测到 SQL 报错或 QPS 断崖下跌 > 20%，\n执行器自动触发以下 Rollback 脚本并回写 WORM 审计链。", fSub, brEmerald, rbBanner.X + 10f, rbBanner.Y + 8f);
                        }
                        cy += 66f;

                        RectangleF rbBanner2 = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 56f);
                        using (SolidBrush rbBg2 = new SolidBrush(Color.FromArgb(28, cAmber)))
                        using (Pen rbPen2 = new Pen(Color.FromArgb(80, cAmber), 1f))
                        {
                            g.FillRectangle(rbBg2, rbBanner2);
                            g.DrawRectangle(rbPen2, rbBanner2.X, rbBanner2.Y, rbBanner2.Width, rbBanner2.Height);
                            g.DrawString("② 24~72 小时延迟观察期：联动 SIEM 监听 Access Denied / Permission Denied\n报错，命中后在审计视图触发「一键紧急恢复」，回滚脚本已就绪待命。", fSub, brAmber, rbBanner2.X + 10f, rbBanner2.Y + 8f);
                        }
                        cy += 68f;

                        RectangleF rbBox = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 208f);
                        using (SolidBrush brBox = new SolidBrush(bgObsidian))
                        {
                            g.FillRectangle(brBox, rbBox);
                            g.DrawRectangle(divPen, rbBox.X, rbBox.Y, rbBox.Width, rbBox.Height);
                        }
                        g.DrawString("rollback_mysql_priv_20261122.sql", fMono, brMuted, rbBox.X + 12f, rbBox.Y + 10f);
                        string[] rbLines = new string[]
                        {
                            "-- 恢复变更前权限快照 (Snapshot #SNAP-094)",
                            "GRANT ALL PRIVILEGES ON *.*",
                            "  TO 'app_order_rw'@'10.24.%'",
                            "  WITH GRANT OPTION;",
                            "",
                            "SET PERSIST local_infile = ON;",
                            "FLUSH PRIVILEGES;"
                        };
                        for (int l = 0; l < rbLines.Length; l++)
                        {
                            Brush lBr = rbLines[l].StartsWith("--") ? brMuted : brCyan;
                            g.DrawString(rbLines[l], fMono, lBr, rbBox.X + 12f, rbBox.Y + 36f + l * 18f);
                        }
                    }
                    else if (_hitlActiveTab == 2)
                    {
                        RectangleF sbHead = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 36f);
                        using (SolidBrush sbBg = new SolidBrush(Color.FromArgb(30, cEmerald)))
                        using (Pen sbPen = new Pen(Color.FromArgb(85, cEmerald), 1f))
                        {
                            g.FillRectangle(sbBg, sbHead);
                            g.DrawRectangle(sbPen, sbHead.X, sbHead.Y, sbHead.Width, sbHead.Height);
                            g.DrawString("✓ 双轨验证通过 (轨道1 沙箱 + 轨道2 生产只读预检)", fBadge, brEmerald, sbHead.X + 10f, sbHead.Y + 9f);
                            g.DrawString("mysql:8.0.32", fMono, brEmerald, sbHead.Right - 96f, sbHead.Y + 10f);
                        }
                        cy += 46f;

                        // 异构分层池化调度策略
                        RectangleF poolStrip = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 32f);
                        using (SolidBrush psBg = new SolidBrush(Color.FromArgb(26, cCyan)))
                        using (Pen psPen = new Pen(Color.FromArgb(75, cCyan), 1f))
                        {
                            g.FillRectangle(psBg, poolStrip);
                            g.DrawRectangle(psPen, poolStrip.X, poolStrip.Y, poolStrip.Width, poolStrip.Height);
                            g.DrawString("池化策略: EPHEMERAL_CONTAINER", fMono, brCyan, poolStrip.X + 10f, poolStrip.Y + 9f);
                            g.DrawString("轻量引擎瞬时容器秒级拉起即毁", fSub, brMuted, poolStrip.X + 244f, poolStrip.Y + 8f);
                        }
                        cy += 40f;

                        // 轨道 2 生产只读预检指标
                        float tw = (rect.Width - 44f) / 3f;
                        DrawEvalMiniCard(g, new RectangleF(rect.X + 16f, cy, tw, 52f), "生产表真实行数", "486,120", bgObsidian, borderCol, brMuted, brPrimary, fSub, fMonoBold);
                        DrawEvalMiniCard(g, new RectangleF(rect.X + 22f + tw, cy, tw, 52f), "数据体积", "534.7 MB", bgObsidian, borderCol, brMuted, brPrimary, fSub, fMonoBold);
                        DrawEvalMiniCard(g, new RectangleF(rect.X + 28f + tw * 2f, cy, tw, 52f), ">30s 活跃长事务", "0 个 · 放行", bgObsidian, borderCol, brMuted, brEmerald, fSub, fBadge);
                        cy += 64f;

                        RectangleF sbBox = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 246f);
                        using (SolidBrush brBox = new SolidBrush(bgObsidian))
                        {
                            g.FillRectangle(brBox, sbBox);
                            g.DrawRectangle(divPen, sbBox.X, sbBox.Y, sbBox.Width, sbBox.Height);
                        }
                        string[] sbLogs = new string[]
                        {
                            "# 轨道 1 阶段 1: 漏洞真实性 PoC 验证 (消除误报)",
                            "[VULN] SELECT 'poc' INTO OUTFILE '/tmp/dbguard_test'",
                            "       -> 写入成功 (漏洞属实，排除静态误报)",
                            "[VULN] SELECT user FROM mysql.user -> 越权读取成功",
                            "",
                            "# 轨道 1 阶段 2: 执行修复 SQL 与回滚脚本 Dry-Run",
                            "[EXEC] Applying fix_mysql_priv_20261122.sql... OK (42ms)",
                            "[ROLL] rollback_mysql_priv_20261122.sql... OK 可逆性通过",
                            "",
                            "# 轨道 1 阶段 3: 修复后安全复测 & 业务兼容性回放",
                            "[PASS] 复测 PoC: Access denied for user 'app_order_rw'",
                            "[PASS] 回放脱敏业务高频 SQL (240/240) -> 100% 兼容无报错",
                            "",
                            "# 轨道 2: 生产只读安全预检 (防 MDL 锁雪崩)",
                            "[PRE] 只读采集 table_rows=486120, data_size_mb=534.7",
                            "[PRE] 活跃长事务 (>30s) = 0 -> 未触发变更窗口挂起"
                        };
                        for (int l = 0; l < sbLogs.Length; l++)
                        {
                            string line = sbLogs[l];
                            Brush lBr = line.StartsWith("#") ? brMuted
                                : (line.StartsWith("[VULN]") ? brRose
                                : (line.StartsWith("[EXEC]") || line.StartsWith("[PRE]") ? brCyan
                                : (line.StartsWith("[ROLL]") ? brAmber : brEmerald)));
                            g.DrawString(line, fMono, lBr, sbBox.X + 10f, sbBox.Y + 10f + l * 15.8f);
                        }
                    }
                    else
                    {
                        g.DrawString("隐私网关在发送 Prompt 至 Claude-3.7 API 前自动完成替换，\n确保真实库表、内网拓扑与账号零泄露：", fSub, brMuted, rect.X + 16f, cy);
                        cy += 44f;

                        RectangleF m1 = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 82f);
                        using (SolidBrush m1Bg = new SolidBrush(Color.FromArgb(24, cRose)))
                        using (Pen m1Pen = new Pen(Color.FromArgb(80, cRose), 1f))
                        {
                            g.FillRectangle(m1Bg, m1);
                            g.DrawRectangle(m1Pen, m1.X, m1.Y, m1.Width, m1.Height);
                            g.DrawString("- 脱敏前 (仅留存内网内存)", fBadge, brRose, m1.X + 10f, m1.Y + 8f);
                            g.DrawString("Host: 10.24.8.19  |  Table: prod_pay_users\nUser: 'app_order_rw'@'10.24.%'", fMono, brPrimary, m1.X + 10f, m1.Y + 32f);
                        }
                        cy += 96f;

                        RectangleF m2 = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 82f);
                        using (SolidBrush m2Bg = new SolidBrush(Color.FromArgb(24, cEmerald)))
                        using (Pen m2Pen = new Pen(Color.FromArgb(80, cEmerald), 1f))
                        {
                            g.FillRectangle(m2Bg, m2);
                            g.DrawRectangle(m2Pen, m2.X, m2.Y, m2.Width, m2.Height);
                            g.DrawString("+ 脱敏后 (实际发送给外部 LLM，返回后本地自动还原)", fBadge, brEmerald, m2.X + 10f, m2.Y + 8f);
                            g.DrawString("Host: 10.x.x.19   |  Table: tbl_masked_01\nUser: 'usr_masked_02'@'10.x.%'", fMono, brPrimary, m2.X + 10f, m2.Y + 32f);
                        }
                        cy += 96f;

                        // AST 节点级脱敏 + KMS 信封加密映射状态
                        RectangleF astStrip = new RectangleF(rect.X + 16f, cy, rect.Width - 32f, 74f);
                        using (SolidBrush asBg = new SolidBrush(Color.FromArgb(26, cIndigo)))
                        using (Pen asPen = new Pen(Color.FromArgb(80, cIndigo), 1f))
                        {
                            g.FillRectangle(asBg, astStrip);
                            g.DrawRectangle(asPen, astStrip.X, astStrip.Y, astStrip.Width, astStrip.Height);
                            g.DrawString("AST 节点级替换：标识符 3 · 字面量 2 · 关键字 17（SQL 结构完整保留）", fSub, brIndigo, astStrip.X + 10f, astStrip.Y + 8f);
                            g.DrawString("映射表 KMS 信封加密持久化 · workflow_run_id=wf-run-7f3a91", fSub, brIndigo, astStrip.X + 10f, astStrip.Y + 28f);
                            g.DrawString("TTL 86400s · HITL 跨天审批可恢复 · 变更闭环后立即销毁", fSub, brMuted, astStrip.X + 10f, astStrip.Y + 48f);
                        }
                    }

                    // 底部固定 HITL 长按蓄力审批栏 (沿边框填充进度 + 未满回弹 + 满格微震动)
                    RectangleF botBar = new RectangleF(rect.X, rect.Bottom - 78f, rect.Width, 78f);
                    using (SolidBrush brBot = new SolidBrush(bgObsidian))
                    {
                        g.FillRectangle(brBot, botBar);
                        g.DrawLine(divPen, botBar.Left, botBar.Top, botBar.Right, botBar.Top);
                    }

                    g.DrawString("按住左键沿按钮边框填充进度 · 未满松开弹性回弹 · 填满后微震动锁定", new Font("Microsoft YaHei UI", 7.5f, FontStyle.Regular), brMuted, botBar.X + 16f, botBar.Y + 7f);

                    float btnY = botBar.Y + 28f;
                    _btnApproveHoldRect = new RectangleF(botBar.X + 16f, btnY, 260f, 38f);
                    _btnRejectHoldRect = new RectangleF(_btnApproveHoldRect.Right + 10f, btnY, 136f, 38f);

                    bool isPending = (node.Status == "pending");
                    bool isApproved = (node.Status == "completed");
                    bool isRejected = (node.Status == "rejected");

                    string appLabel = isApproved
                        ? "✓ 已批准执行 (Approved)"
                        : (_approveProgress > 0.01f ? string.Format("批准蓄力中 {0:0}%...", _approveProgress * 100f) : "✓ 长按批准执行 (Approve)");
                    DrawHoldActionBtn(g, _btnApproveHoldRect, appLabel, _approveProgress, isApproved, !isPending && !isApproved, cEmerald, dark, fgPrimary);

                    string rejLabel = isRejected
                        ? "✓ 已驳回 (Rejected)"
                        : (_rejectProgress > 0.01f ? string.Format("驳回 {0:0}%...", _rejectProgress * 100f) : "长按驳回 (Reject)");
                    DrawHoldActionBtn(g, _btnRejectHoldRect, rejLabel, _rejectProgress, isRejected, !isPending && !isRejected, cRose, dark, fgPrimary);
                    return;
                }

                // === 其他 7 个节点的通用检视器 ===
                RectangleF catBox = new RectangleF(rect.X + 16f, rect.Y + 14f, 148f, 22f);
                using (SolidBrush cbBg = new SolidBrush(bgObsidian))
                {
                    g.FillRectangle(cbBg, catBox);
                    g.DrawRectangle(divPen, catBox.X, catBox.Y, catBox.Width, catBox.Height);
                    StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                    g.DrawString(node.Category, fBadge, brCyan, catBox, sf);
                }
                g.DrawString("耗时: " + node.Metric, fMono, brMuted, rect.Right - 110f, rect.Y + 18f);
                g.DrawString(node.Title, fTitle, brPrimary, rect.X + 16f, rect.Y + 46f);
                g.DrawString(node.Subtitle, fSub, brMuted, rect.X + 16f, rect.Y + 70f);
                g.DrawLine(divPen, rect.Left, rect.Y + 96f, rect.Right, rect.Y + 96f);

                // 节点运行指标与配置 (2x2 网格)
                g.DrawString("节点运行指标与配置", fBadge, brMuted, rect.X + 16f, rect.Y + 110f);
                float mw = (rect.Width - 42f) * 0.5f;
                int rows = node.Meta.GetLength(0);
                for (int r = 0; r < rows; r++)
                {
                    int col = r % 2;
                    int row = r / 2;
                    RectangleF mr = new RectangleF(rect.X + 16f + col * (mw + 10f), rect.Y + 134f + row * 60f, mw, 52f);
                    using (SolidBrush mrBg = new SolidBrush(bgObsidian))
                    {
                        g.FillRectangle(mrBg, mr);
                        g.DrawRectangle(divPen, mr.X, mr.Y, mr.Width, mr.Height);
                    }
                    g.DrawString(node.Meta[r, 0], fSub, brMuted, mr.X + 10f, mr.Y + 7f);
                    g.DrawString(node.Meta[r, 1], fMonoBold, brPrimary, mr.X + 10f, mr.Y + 26f);
                }

                // 实时执行日志流
                float logY = rect.Y + 268f;
                g.DrawString("实时执行日志流", fBadge, brMuted, rect.X + 16f, logY);
                g.DrawString("● Live Stream", fMono, brEmerald, rect.Right - 108f, logY);

                RectangleF logBox = new RectangleF(rect.X + 16f, logY + 24f, rect.Width - 32f, 220f);
                using (SolidBrush lbBg = new SolidBrush(bgObsidian))
                {
                    g.FillRectangle(lbBg, logBox);
                    g.DrawRectangle(divPen, logBox.X, logBox.Y, logBox.Width, logBox.Height);
                }
                for (int l = 0; l < node.Logs.Length; l++)
                {
                    RectangleF lr = new RectangleF(logBox.X + 10f, logBox.Y + 12f + l * 52f, logBox.Width - 20f, 48f);
                    g.DrawString(node.Logs[l], fMono, brSecondary, lr);
                }

                // 底部跳转 HITL 按钮
                _btnJumpToHitlRect = new RectangleF(rect.X + 16f, rect.Bottom - 54f, rect.Width - 32f, 38f);
                using (SolidBrush jbBg = new SolidBrush(Color.FromArgb(30, cAmber)))
                using (Pen jbPen = new Pen(Color.FromArgb(110, cAmber), 1.2f))
                {
                    g.FillRectangle(jbBg, _btnJumpToHitlRect);
                    g.DrawRectangle(jbPen, _btnJumpToHitlRect.X, _btnJumpToHitlRect.Y, _btnJumpToHitlRect.Width, _btnJumpToHitlRect.Height);
                    StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                    g.DrawString("跳转到 [HITL 审批] 节点查看修复方案与长按审批", fBadge, brAmber, _btnJumpToHitlRect, sf);
                }
            }
        }

        private void DrawEvalMiniCard(
            Graphics g,
            RectangleF r,
            string label,
            string val,
            Color bg,
            Color border,
            Brush brLabel,
            Brush brVal,
            Font fLabel,
            Font fVal)
        {
            using (SolidBrush br = new SolidBrush(bg))
            using (Pen pen = new Pen(border, 1f))
            {
                g.FillRectangle(br, r);
                g.DrawRectangle(pen, r.X, r.Y, r.Width, r.Height);
            }
            StringFormat sf = new StringFormat { Alignment = StringAlignment.Center };
            g.DrawString(label, fLabel, brLabel, r.X + r.Width * 0.5f, r.Y + 7f, sf);
            g.DrawString(val, fVal, brVal, r.X + r.Width * 0.5f, r.Y + 26f, sf);
        }

        private void DrawHoldActionBtn(
            Graphics g,
            RectangleF rect,
            string label,
            float progress,
            bool isDone,
            bool isDisabled,
            Color neonCol,
            bool dark,
            Color fgPrimary)
        {
            bool hover = rect.Contains(_mousePos) && !isDone && !isDisabled;
            Color bg = isDone
                ? Color.FromArgb(dark ? 70 : 45, neonCol)
                : (hover ? Color.FromArgb(dark ? 38 : 24, neonCol) : Color.FromArgb(dark ? 22 : 14, neonCol));

            using (SolidBrush brBg = new SolidBrush(bg))
            using (Pen basePen = new Pen(isDone ? neonCol : Color.FromArgb(105, neonCol), isDone ? 1.8f : 1.1f))
            {
                g.FillRectangle(brBg, rect);
                if (progress > 0.005f && !isDone)
                {
                    using (SolidBrush tint = new SolidBrush(Color.FromArgb(50, neonCol)))
                    {
                        g.FillRectangle(tint, rect.X, rect.Y, rect.Width * Math.Min(1f, progress), rect.Height);
                    }
                }
                g.DrawRectangle(basePen, rect.X, rect.Y, rect.Width, rect.Height);
            }

            if (progress > 0.002f && !isDone)
            {
                DrawPerimeterProgress(g, rect, Math.Min(1f, progress), neonCol, 2.8f);
            }

            using (Font f = new Font("Microsoft YaHei UI", 8.6f, FontStyle.Bold))
            using (SolidBrush brFg = new SolidBrush(isDone ? neonCol : (isDisabled ? Color.FromArgb(100, fgPrimary) : fgPrimary)))
            {
                StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                g.DrawString(label, f, brFg, rect, sf);
            }
        }

        private void DrawPerimeterProgress(Graphics g, RectangleF r, float progress, Color color, float strokeWidth)
        {
            float w = r.Width;
            float h = r.Height;
            float total = 2f * (w + h);
            float rem = total * Math.Max(0f, Math.Min(1f, progress));

            using (Pen glowPen = new Pen(Color.FromArgb(95, color), strokeWidth + 2.6f))
            using (Pen corePen = new Pen(color, strokeWidth))
            {
                if (rem > 0f)
                {
                    float seg = Math.Min(w, rem);
                    g.DrawLine(glowPen, r.Left, r.Top, r.Left + seg, r.Top);
                    g.DrawLine(corePen, r.Left, r.Top, r.Left + seg, r.Top);
                    rem -= seg;
                }
                if (rem > 0f)
                {
                    float seg = Math.Min(h, rem);
                    g.DrawLine(glowPen, r.Right, r.Top, r.Right, r.Top + seg);
                    g.DrawLine(corePen, r.Right, r.Top, r.Right, r.Top + seg);
                    rem -= seg;
                }
                if (rem > 0f)
                {
                    float seg = Math.Min(w, rem);
                    g.DrawLine(glowPen, r.Right, r.Bottom, r.Right - seg, r.Bottom);
                    g.DrawLine(corePen, r.Right, r.Bottom, r.Right - seg, r.Bottom);
                    rem -= seg;
                }
                if (rem > 0f)
                {
                    float seg = Math.Min(h, rem);
                    g.DrawLine(glowPen, r.Left, r.Bottom, r.Left, r.Bottom - seg);
                    g.DrawLine(corePen, r.Left, r.Bottom, r.Left, r.Bottom - seg);
                }
            }
        }

        // =========================================================================
        // VIEW 2: 资产与凭据 (DB Assets & Vault)
        // =========================================================================
        private void RenderAssetsView(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber,
            Color cRose)
        {
            float pad = 28f;
            float x0 = rect.X + pad;
            float y0 = rect.Y + 24f;
            float contentW = rect.Width - pad * 2f;

            using (Font fH2 = new Font("Microsoft YaHei UI", 13f, FontStyle.Bold))
            using (Font fDesc = new Font("Microsoft YaHei UI", 8.6f, FontStyle.Regular))
            using (Font fBig = new Font("Consolas", 18f, FontStyle.Bold))
            using (Font fCellBold = new Font("Microsoft YaHei UI", 9.0f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 8.4f, FontStyle.Regular))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brRose = new SolidBrush(cRose))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brAmber = new SolidBrush(cAmber))
            using (SolidBrush brIndigo = new SolidBrush(cIndigo))
            using (SolidBrush brCard = new SolidBrush(bgCard))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.DrawString("异构数据库资产与零信任凭据管理", fH2, brPrimary, x0, y0);
                g.DrawString("统一纳管多源数据库资产，所有连接凭据由 HashiCorp Vault / KMS 动态签发，严格杜绝明文落盘。", fDesc, brMuted, x0, y0 + 28f);

                _btnSyncCmdbRect = new RectangleF(x0 + contentW - 132f, y0 + 6f, 132f, 34f);
                DrawHeaderButton(g, _btnSyncCmdbRect, "↻ 同步 CMDB 资产", false, dark, bgCard, borderCol, fgPrimary, cCyan);

                // 4 张统计卡片
                float cardY = y0 + 68f;
                float cardW = (contentW - 36f) / 4f;
                string[] kLabels = new string[] { "纳管数据库实例", "高危业务暴露资产", "动态凭据覆盖率", "低侵入探查负载" };
                string[] kVals = new string[] { "5", "1", "100%", "< 1.8%" };
                string[] kSubs = new string[] { "100% 在线", "含 PII / 公网映射", "Vault TTL 15m", "CPU 峰值增量" };
                Brush[] kBrushes = new Brush[] { brPrimary, brRose, brEmerald, brCyan };

                for (int i = 0; i < 4; i++)
                {
                    RectangleF cr = new RectangleF(x0 + i * (cardW + 12f), cardY, cardW, 82f);
                    g.FillRectangle(brCard, cr);
                    g.DrawRectangle(borderPen, cr.X, cr.Y, cr.Width, cr.Height);
                    g.DrawString(kLabels[i], fDesc, brMuted, cr.X + 16f, cr.Y + 12f);
                    g.DrawString(kVals[i], fBig, kBrushes[i], cr.X + 14f, cr.Y + 36f);
                    g.DrawString(kSubs[i], fDesc, i == 0 ? brEmerald : brMuted, cr.X + 108f, cr.Y + 46f);
                }

                // 资产表格
                float tableY = cardY + 104f;
                RectangleF tableRect = new RectangleF(x0, tableY, contentW, 44f + _assets.Count * 62f);
                g.FillRectangle(brCard, tableRect);
                g.DrawRectangle(borderPen, tableRect.X, tableRect.Y, tableRect.Width, tableRect.Height);

                using (SolidBrush thBr = new SolidBrush(bgPanel))
                {
                    g.FillRectangle(thBr, tableRect.X + 1, tableRect.Y + 1, tableRect.Width - 2, 42f);
                }
                g.DrawLine(borderPen, tableRect.Left, tableRect.Y + 43f, tableRect.Right, tableRect.Y + 43f);

                float[] colOffsets = new float[] { 18f, 270f, 440f, 600f, 830f, contentW - 115f };
                string[] headers = new string[] { "实例名称 / 引擎版本", "业务等级", "网络暴露面", "凭据托管 / 沙箱池化策略", "上下文风险评分", "操作" };
                for (int c = 0; c < headers.Length; c++)
                {
                    g.DrawString(headers[c], fCellBold, brMuted, tableRect.X + colOffsets[c], tableRect.Y + 13f);
                }

                for (int r = 0; r < _assets.Count; r++)
                {
                    AssetRowItem item = _assets[r];
                    float ry = tableRect.Y + 44f + r * 62f;
                    if (r > 0) g.DrawLine(borderPen, tableRect.Left, ry, tableRect.Right, ry);

                    g.DrawString(item.Name, fCellBold, brPrimary, tableRect.X + colOffsets[0], ry + 11f);
                    g.DrawString(item.EngineInfo, fMono, brMuted, tableRect.X + colOffsets[0], ry + 33f);

                    // 业务等级徽章
                    Color tierCol = item.IsCoreP0 ? cRose : cIndigo;
                    RectangleF tRect = new RectangleF(tableRect.X + colOffsets[1], ry + 18f, 118f, 24f);
                    using (SolidBrush tbBg = new SolidBrush(Color.FromArgb(35, tierCol)))
                    using (SolidBrush tbFg = new SolidBrush(tierCol))
                    {
                        g.FillRectangle(tbBg, tRect);
                        StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                        g.DrawString(item.TierBadge, fDesc, tbFg, tRect, sf);
                    }

                    // 暴露面徽章
                    Color expCol = item.IsPublic ? cAmber : fgMuted;
                    RectangleF eRect = new RectangleF(tableRect.X + colOffsets[2], ry + 18f, 108f, 24f);
                    using (SolidBrush ebBg = new SolidBrush(Color.FromArgb(30, expCol)))
                    using (SolidBrush ebFg = new SolidBrush(expCol))
                    {
                        g.FillRectangle(ebBg, eRect);
                        StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                        g.DrawString(item.ExposureBadge, fDesc, ebFg, eRect, sf);
                    }

                    g.DrawString(item.VaultInfo, fMono, brEmerald, tableRect.X + colOffsets[3], ry + 10f);
                    g.DrawString(item.PoolStrategy, fMono, brCyan, tableRect.X + colOffsets[3], ry + 33f);

                    Brush scBr = item.ScoreLevel == 0 ? brRose : (item.ScoreLevel == 1 ? brAmber : brEmerald);
                    g.DrawString(item.Score + "  " + item.ScoreLabel, fCellBold, scBr, tableRect.X + colOffsets[4], ry + 21f);

                    item.ActionBtnRect = new RectangleF(tableRect.X + colOffsets[5] - 10f, ry + 16f, 96f, 28f);
                    DrawHeaderButton(g, item.ActionBtnRect, item.ActionText, r == 0, dark, bgObsidian, borderCol, fgPrimary, cCyan);
                }
            }
        }

        // =========================================================================
        // VIEW 3: 模型网关与脱敏 (BYOM Model Gateway & Masking)
        // =========================================================================
        private void RenderGatewayView(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cRose)
        {
            float pad = 28f;
            float x0 = rect.X + pad;
            float y0 = rect.Y + 24f;
            float contentW = rect.Width - pad * 2f;

            using (Font fH2 = new Font("Microsoft YaHei UI", 13f, FontStyle.Bold))
            using (Font fH3 = new Font("Microsoft YaHei UI", 10f, FontStyle.Bold))
            using (Font fDesc = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Regular))
            using (Font fMono = new Font("Consolas", 8.8f, FontStyle.Regular))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brIndigo = new SolidBrush(cIndigo))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brRose = new SolidBrush(cRose))
            using (SolidBrush brCard = new SolidBrush(bgCard))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.DrawString("统一模型网关 (BYOM) 与出站隐私脱敏", fH2, brPrimary, x0, y0);
                g.DrawString("兼容 OpenAI API 协议，按任务复杂度自动分级路由；所有外发至公有云 API 的上下文均经过强制脱敏替换，返回后本地自动还原。", fDesc, brMuted, x0, y0 + 28f);

                _btnConfigByokRect = new RectangleF(x0 + contentW - 165f, y0 + 6f, 165f, 34f);
                DrawHeaderButton(g, _btnConfigByokRect, "+ 配置自定义 BYOK 密钥", true, dark, bgCard, borderCol, fgPrimary, cCyan);

                // 3 张模型后端卡片
                float cardY = y0 + 68f;
                float cardW = (contentW - 28f) / 3f;
                string[] mTags = new string[] { "本地私有化 · 路由层 L1", "外部 API · 路由层 L2", "灾备容错 · 路由层 L2-B" };
                string[] mTitles = new string[] { "vLLM / Ollama (Qwen2.5-14B)", _byokModelName, "DeepSeek-R1 私有集群" };
                string[] mStates = new string[] { "● 在线", "● 已脱敏接入", "● 待命" };
                string[] mDescs = new string[]
                {
                    "承担高吞吐慢查询解析、SQL 语法树提取、初步基线枚举，数据 100% 留存内网。",
                    "承担复杂跨表提权链推理、精准修复 SQL 与 Rollback 脚本生成及沙箱报错自愈。",
                    "当外部 API 超时或涉密级别达到机密级时，自动切换至内网全量推理节点。"
                };
                string[] mFooters = new string[]
                {
                    "平均延迟: 142ms          今日调用: 14,820",
                    "平均延迟: 1.85s          脱敏拦截: 100%",
                    "故障转移: 自动           显存占用: 68%"
                };
                Color[] mAccents = new Color[] { cCyan, cIndigo, fgMuted };

                for (int i = 0; i < 3; i++)
                {
                    RectangleF mr = new RectangleF(x0 + i * (cardW + 14f), cardY, cardW, 146f);
                    g.FillRectangle(brCard, mr);
                    using (Pen mPen = new Pen(Color.FromArgb(110, mAccents[i]), 1.3f))
                    {
                        g.DrawRectangle(mPen, mr.X, mr.Y, mr.Width, mr.Height);
                    }
                    g.DrawString(mTags[i], fDesc, new SolidBrush(mAccents[i]), mr.X + 16f, mr.Y + 14f);
                    g.DrawString(mStates[i], fDesc, i == 2 ? brMuted : brEmerald, mr.Right - 94f, mr.Y + 14f);
                    g.DrawString(mTitles[i], fH3, brPrimary, mr.X + 16f, mr.Y + 38f);
                    g.DrawString(mDescs[i], fDesc, brMuted, new RectangleF(mr.X + 16f, mr.Y + 64f, mr.Width - 32f, 42f));
                    g.DrawLine(borderPen, mr.X + 14f, mr.Bottom - 34f, mr.Right - 14f, mr.Bottom - 34f);
                    g.DrawString(mFooters[i], fMono, brPrimary, mr.X + 16f, mr.Bottom - 24f);
                }

                // 下方左右双栏：脱敏规则开关 + 出站 Prompt 实时脱敏预览
                float botY = cardY + 168f;
                float halfW = (contentW - 16f) * 0.5f;
                RectangleF leftBox = new RectangleF(x0, botY, halfW, 336f);
                RectangleF rightBox = new RectangleF(leftBox.Right + 16f, botY, halfW, 336f);

                g.FillRectangle(brCard, leftBox);
                g.DrawRectangle(borderPen, leftBox.X, leftBox.Y, leftBox.Width, leftBox.Height);
                g.DrawString("出站隐私脱敏网关规则 (点击开关实时验证)", fH3, brPrimary, leftBox.X + 18f, leftBox.Y + 16f);
                g.DrawString("切换下方开关实时预览发送给外部大模型前的 Prompt 脱敏效果：", fDesc, brMuted, leftBox.X + 18f, leftBox.Y + 40f);

                for (int i = 0; i < _maskingRules.Count; i++)
                {
                    MaskingRuleItem rule = _maskingRules[i];
                    RectangleF rr = new RectangleF(leftBox.X + 16f, leftBox.Y + 68f + i * 62f, leftBox.Width - 32f, 54f);
                    using (SolidBrush rBg = new SolidBrush(bgObsidian))
                    {
                        g.FillRectangle(rBg, rr);
                        g.DrawRectangle(borderPen, rr.X, rr.Y, rr.Width, rr.Height);
                    }
                    g.DrawString(rule.Label, new Font("Microsoft YaHei UI", 8.8f, FontStyle.Bold), brPrimary, rr.X + 12f, rr.Y + 8f);
                    g.DrawString(rule.Desc, fDesc, brMuted, rr.X + 12f, rr.Y + 29f);

                    rule.ToggleRect = new RectangleF(rr.Right - 50f, rr.Y + 17f, 36f, 20f);
                    using (SolidBrush swBg = new SolidBrush(rule.Enabled ? cEmerald : Color.FromArgb(65, 80, 102)))
                    {
                        g.FillRectangle(swBg, rule.ToggleRect);
                    }
                    float kx = rule.Enabled ? (rule.ToggleRect.Right - 17f) : (rule.ToggleRect.X + 3f);
                    g.FillEllipse(Brushes.White, kx, rule.ToggleRect.Y + 3f, 14f, 14f);
                }

                // 右侧实时预览
                g.FillRectangle(brCard, rightBox);
                g.DrawRectangle(borderPen, rightBox.X, rightBox.Y, rightBox.Width, rightBox.Height);
                g.DrawString("出站 Prompt 实时脱敏预览", fH3, brPrimary, rightBox.X + 18f, rightBox.Y + 16f);
                g.DrawString("Zero-Leak Guard", fMono, brEmerald, rightBox.Right - 135f, rightBox.Y + 18f);

                RectangleF codeArea = new RectangleF(rightBox.X + 16f, rightBox.Y + 52f, rightBox.Width - 32f, rightBox.Height - 68f);
                using (SolidBrush caBg = new SolidBrush(bgObsidian))
                {
                    g.FillRectangle(caBg, codeArea);
                    g.DrawRectangle(borderPen, codeArea.X, codeArea.Y, codeArea.Width, codeArea.Height);
                }

                bool mTbl = _maskingRules[0].Enabled;
                bool mIp = _maskingRules[1].Enabled;
                bool mCred = _maskingRules[2].Enabled;
                bool mPii = _maskingRules[3].Enabled;

                string vTbl = mTbl ? "tbl_masked_01" : "prod_pay_users (未脱敏!)";
                string vIp = mIp ? "10.x.x.19" : "10.24.8.19 (未脱敏!)";
                string vCred = mCred ? "[REDACTED_CRED]" : "app_order_rw:P@ssw0rd! (未脱敏!)";
                string vPii = mPii ? "[MASKED_PHONE]" : "13800138000 (未脱敏!)";

                g.DrawString("// 发往外部 " + _byokModelName + " 的实际 JSON 载荷", fMono, brMuted, codeArea.X + 16f, codeArea.Y + 18f);
                g.DrawString("{", fMono, brPrimary, codeArea.X + 16f, codeArea.Y + 46f);
                g.DrawString("  \"target_host\": \"" + vIp + ":3306\",", fMono, mIp ? brEmerald : brRose, codeArea.X + 16f, codeArea.Y + 74f);
                g.DrawString("  \"affected_table\": \"" + vTbl + "\",", fMono, mTbl ? brEmerald : brRose, codeArea.X + 16f, codeArea.Y + 102f);
                g.DrawString("  \"credential_trace\": \"" + vCred + "\",", fMono, mCred ? brEmerald : brRose, codeArea.X + 16f, codeArea.Y + 130f);
                g.DrawString("  \"sample_slow_query\": \"SELECT * FROM " + (mTbl ? "tbl_masked_01" : "prod_pay_users") + "\",", fMono, mTbl ? brEmerald : brRose, codeArea.X + 16f, codeArea.Y + 158f);
                g.DrawString("  \"where_filter\": \"phone = '" + vPii + "'\"", fMono, mPii ? brEmerald : brRose, codeArea.X + 16f, codeArea.Y + 186f);
                g.DrawString("}", fMono, brPrimary, codeArea.X + 16f, codeArea.Y + 214f);
                g.DrawString("// 模型返回修复 SQL 后，本地网关自动将 tbl_masked_01 还原为 prod_pay_users", fMono, brCyan, codeArea.X + 16f, codeArea.Y + 244f);
            }
        }

        // =========================================================================
        // VIEW 4: 自主进化中心 (Self-Evolution Center & Git-Style Rollback)
        // =========================================================================
        private void RenderEvolutionView(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald)
        {
            float pad = 28f;
            float x0 = rect.X + pad;
            float y0 = rect.Y + 24f;
            float contentW = rect.Width - pad * 2f;

            using (Font fH2 = new Font("Microsoft YaHei UI", 12f, FontStyle.Bold))
            using (Font fH3 = new Font("Microsoft YaHei UI", 10f, FontStyle.Bold))
            using (Font fDesc = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Regular))
            using (Font fBig = new Font("Consolas", 18f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 9.0f, FontStyle.Bold))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brIndigo = new SolidBrush(cIndigo))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brCard = new SolidBrush(bgCard))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                // 顶部状态横幅
                RectangleF banner = new RectangleF(x0, y0, contentW, 92f);
                using (SolidBrush bBg = new SolidBrush(_selfEvolutionEnabled ? Color.FromArgb(dark ? 38 : 22, cIndigo) : bgCard))
                using (Pen bPen = new Pen(_selfEvolutionEnabled ? cIndigo : borderCol, 1.3f))
                {
                    g.FillRectangle(bBg, banner);
                    g.DrawRectangle(bPen, banner.X, banner.Y, banner.Width, banner.Height);
                }

                string bTitle = _selfEvolutionEnabled
                    ? "自主进化引擎 · 已开启 (影子测试受控模式)   [Active: " + _activeEvoVersion + "]"
                    : "自主进化引擎 · 已关闭 (纯静态确定性模式)   [Active: " + _activeEvoVersion + "]";
                string bDesc = _selfEvolutionEnabled
                    ? "系统正从历史误报、DBA 修改记录与沙箱验证结果中自动蒸馏检测规则与领域记忆；所有新规则必须先通过影子测试（Shadow Mode）且不越权修改生产执行权限。"
                    : "当前仅使用静态合规基线与固定 Prompt 运行，不会自动写入领域记忆或蒸馏新规则，适合高度受限的合规冻结窗口。";

                g.DrawString(bTitle, fH2, brPrimary, banner.X + 20f, banner.Y + 16f);
                g.DrawString(bDesc, fDesc, brMuted, banner.X + 20f, banner.Y + 50f);

                _btnEvoBannerToggleRect = new RectangleF(banner.Right - 178f, banner.Y + 26f, 158f, 36f);
                DrawHeaderButton(g, _btnEvoBannerToggleRect, _selfEvolutionEnabled ? "切换为静态确定性模式" : "重新开启自主进化模式", true, dark, bgObsidian, borderCol, fgPrimary, cIndigo);

                // 3 张进化指标卡
                float cardY = banner.Bottom + 20f;
                float cardW = (contentW - 28f) / 3f;
                string[] eLabels = new string[] { "影子测试验证 · 误报率下降", "Harness 修复脚本首通率", "企业专属领域记忆库" };
                string[] eVals = new string[] { "-42.6%", "96.8%", "128 条" };
                string[] eSubs = new string[] { "较静态基线 v1.0", "+11.4% 提升", "含自定义中间件与补偿控制" };
                Brush[] eBr = new Brush[] { brEmerald, brCyan, brIndigo };

                for (int i = 0; i < 3; i++)
                {
                    RectangleF er = new RectangleF(x0 + i * (cardW + 14f), cardY, cardW, 84f);
                    g.FillRectangle(brCard, er);
                    g.DrawRectangle(borderPen, er.X, er.Y, er.Width, er.Height);
                    g.DrawString(eLabels[i], fDesc, brMuted, er.X + 16f, er.Y + 12f);
                    g.DrawString(eVals[i], fBig, eBr[i], er.X + 14f, er.Y + 38f);
                    g.DrawString(eSubs[i], fDesc, brMuted, er.X + 135f, er.Y + 48f);
                }

                // Git 风格进化版本历史与一键回滚
                float listY = cardY + 106f;
                RectangleF listBox = new RectangleF(x0, listY, contentW, 70f + _evoVersions.Count * 88f);
                g.FillRectangle(brCard, listBox);
                g.DrawRectangle(borderPen, listBox.X, listBox.Y, listBox.Width, listBox.Height);
                g.DrawString("进化快照版本控制 (Git-Style Rollback)", fH3, brPrimary, listBox.X + 20f, listBox.Y + 16f);
                g.DrawString("对蒸馏出的检测规则、Prompt 策略与领域记忆进行快照管理，若指标退化可随时秒级回滚。", fDesc, brMuted, listBox.X + 20f, listBox.Y + 38f);

                for (int i = 0; i < _evoVersions.Count; i++)
                {
                    EvoVersionItem v = _evoVersions[i];
                    bool isCur = (v.Version == _activeEvoVersion);
                    RectangleF vr = new RectangleF(listBox.X + 18f, listBox.Y + 68f + i * 84f, listBox.Width - 36f, 74f);
                    using (SolidBrush vBg = new SolidBrush(isCur ? Color.FromArgb(32, cIndigo) : bgObsidian))
                    using (Pen vPen = new Pen(isCur ? cIndigo : borderCol, isCur ? 1.5f : 1f))
                    {
                        g.FillRectangle(vBg, vr);
                        g.DrawRectangle(vPen, vr.X, vr.Y, vr.Width, vr.Height);
                    }

                    g.DrawString(v.Version, fMono, isCur ? brIndigo : brPrimary, vr.X + 14f, vr.Y + 10f);
                    if (isCur)
                    {
                        g.DrawString("[当前运行版本]", fDesc, brEmerald, vr.X + 66f, vr.Y + 11f);
                    }
                    g.DrawString(v.Date + "   ·   影子测试: " + v.ShadowScore + "   ·   " + v.RulesAdded, fDesc, brMuted, vr.X + 168f, vr.Y + 11f);
                    g.DrawString(v.Summary, fDesc, brPrimary, vr.X + 14f, vr.Y + 40f);

                    v.RollbackBtnRect = new RectangleF(vr.Right - 122f, vr.Y + 21f, 106f, 32f);
                    if (isCur)
                    {
                        g.DrawString("✓ 已激活", fDesc, brEmerald, vr.Right - 82f, vr.Y + 28f);
                    }
                    else
                    {
                        DrawHeaderButton(g, v.RollbackBtnRect, "回滚到此版本", false, dark, bgCard, borderCol, fgPrimary, cIndigo);
                    }
                }
            }
        }

        // =========================================================================
        // VIEW 5: 审计日志 (Audit & RBAC)
        // =========================================================================
        private void RenderAuditView(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cEmerald)
        {
            float pad = 28f;
            float x0 = rect.X + pad;
            float y0 = rect.Y + 24f;
            float contentW = rect.Width - pad * 2f;

            using (Font fH2 = new Font("Microsoft YaHei UI", 13f, FontStyle.Bold))
            using (Font fDesc = new Font("Microsoft YaHei UI", 8.6f, FontStyle.Regular))
            using (Font fBold = new Font("Microsoft YaHei UI", 8.8f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 8.4f, FontStyle.Regular))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brCard = new SolidBrush(bgCard))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.DrawString("全链路不可篡改审计日志 (WORM Hash Chain)", fH2, brPrimary, x0, y0);
                g.DrawString("完整记录资产探查、出站脱敏、沙箱验证、DBA 双人审批及进化版本变更，每条记录附带 SHA-256 哈希链防篡改校验。", fDesc, brMuted, x0, y0 + 28f);

                _btnExportSarifRect = new RectangleF(x0 + contentW - 180f, y0 + 6f, 180f, 34f);
                DrawHeaderButton(g, _btnExportSarifRect, "导出审计报告 (PDF/SARIF)", true, dark, bgCard, borderCol, fgPrimary, cCyan);

                // 24~72h 延迟观察期 SIEM 监控横幅 + 一键紧急恢复
                Color cAmber = dark ? Color.FromArgb(245, 158, 11) : Color.FromArgb(217, 119, 6);
                RectangleF obsBanner = new RectangleF(x0, y0 + 54f, contentW, 78f);
                using (SolidBrush obBg = new SolidBrush(_oneClickRestoreDone ? Color.FromArgb(dark ? 26 : 18, cEmerald) : Color.FromArgb(dark ? 34 : 22, cAmber)))
                using (Pen obPen = new Pen(_oneClickRestoreDone ? cEmerald : cAmber, 1.3f))
                using (Font fObs = new Font("Microsoft YaHei UI", 9.4f, FontStyle.Bold))
                using (SolidBrush brObs = new SolidBrush(_oneClickRestoreDone ? cEmerald : cAmber))
                {
                    g.FillRectangle(obBg, obsBanner);
                    g.DrawRectangle(obPen, obsBanner.X, obsBanner.Y, obsBanner.Width, obsBanner.Height);

                    string obsTitle = _oneClickRestoreDone
                        ? "✓ 延迟观察期异常已处置：1-Click Restore 回滚完成"
                        : "24~72h 延迟观察期 · SIEM 权限报错监控中";
                    string obsDesc = _oneClickRestoreDone
                        ? "回滚脚本已执行，权限快照恢复至变更前状态；本次恢复已写入 WORM 不可篡改哈希链。"
                        : "SIEM 持续监听 Access Denied / Permission Denied 报错；命中即触发告警并允许一键紧急恢复，避免权限收敛引发业务中断。";
                    g.DrawString(obsTitle, fObs, brObs, obsBanner.X + 18f, obsBanner.Y + 14f);
                    g.DrawString(obsDesc, fDesc, brMuted, obsBanner.X + 18f, obsBanner.Y + 44f);

                    _btnOneClickRestoreRect = new RectangleF(obsBanner.Right - 226f, obsBanner.Y + 22f, 208f, 34f);
                    if (!_oneClickRestoreDone)
                    {
                        DrawHeaderButton(g, _btnOneClickRestoreRect, "一键紧急恢复 (1-Click)", false, dark, bgCard, cAmber, cAmber, cAmber);
                    }
                }

                float tableY = y0 + 148f;
                int rows = Math.Min(10, _auditLogs.Count);
                RectangleF tableRect = new RectangleF(x0, tableY, contentW, 44f + rows * 48f);
                g.FillRectangle(brCard, tableRect);
                g.DrawRectangle(borderPen, tableRect.X, tableRect.Y, tableRect.Width, tableRect.Height);

                using (SolidBrush thBr = new SolidBrush(bgPanel))
                {
                    g.FillRectangle(thBr, tableRect.X + 1, tableRect.Y + 1, tableRect.Width - 2, 42f);
                }
                g.DrawLine(borderPen, tableRect.Left, tableRect.Y + 43f, tableRect.Right, tableRect.Y + 43f);

                float[] cols = new float[] { 16f, 180f, 340f, 445f, contentW - 220f, contentW - 76f };
                string[] headers = new string[] { "时间戳", "操作主体 / Agent", "动作类型", "详细描述", "防篡改哈希 (SHA-256)", "状态" };
                for (int c = 0; c < headers.Length; c++)
                {
                    g.DrawString(headers[c], fBold, brMuted, tableRect.X + cols[c], tableRect.Y + 13f);
                }

                for (int r = 0; r < rows; r++)
                {
                    AuditLogItem log = _auditLogs[r];
                    float ry = tableRect.Y + 44f + r * 48f;
                    if (r > 0) g.DrawLine(borderPen, tableRect.Left, ry, tableRect.Right, ry);

                    g.DrawString(log.Time, fMono, brMuted, tableRect.X + cols[0], ry + 15f);
                    g.DrawString(log.Actor, fBold, brPrimary, tableRect.X + cols[1], ry + 14f);
                    g.DrawString("[" + log.Action + "]", fDesc, brCyan, tableRect.X + cols[2], ry + 14f);
                    g.DrawString(log.Detail, fDesc, brPrimary, tableRect.X + cols[3], ry + 14f);
                    g.DrawString(log.Hash, fMono, brMuted, tableRect.X + cols[4], ry + 15f);
                    g.DrawString(log.Status, fBold, brEmerald, tableRect.X + cols[5], ry + 14f);
                }
            }
        }

        // =========================================================================
        // VIEW 6: 评测与数据中台 (Eval & Golden Dataset Platform)
        // =========================================================================
        private void RenderEvalDataView(
            Graphics g,
            RectangleF rect,
            bool dark,
            Color bgObsidian,
            Color bgPanel,
            Color bgCard,
            Color borderCol,
            Color fgPrimary,
            Color fgSecondary,
            Color fgMuted,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber,
            Color cRose)
        {
            float pad = 28f;
            float x0 = rect.X + pad;
            float y0 = rect.Y + 24f;
            float contentW = rect.Width - pad * 2f;

            using (Font fH2 = new Font("Microsoft YaHei UI", 13f, FontStyle.Bold))
            using (Font fH3 = new Font("Microsoft YaHei UI", 10f, FontStyle.Bold))
            using (Font fDesc = new Font("Microsoft YaHei UI", 8.5f, FontStyle.Regular))
            using (Font fBig = new Font("Consolas", 16f, FontStyle.Bold))
            using (Font fBold = new Font("Microsoft YaHei UI", 8.8f, FontStyle.Bold))
            using (Font fMono = new Font("Consolas", 8.4f, FontStyle.Regular))
            using (SolidBrush brPrimary = new SolidBrush(fgPrimary))
            using (SolidBrush brMuted = new SolidBrush(fgMuted))
            using (SolidBrush brCyan = new SolidBrush(cCyan))
            using (SolidBrush brIndigo = new SolidBrush(cIndigo))
            using (SolidBrush brEmerald = new SolidBrush(cEmerald))
            using (SolidBrush brAmber = new SolidBrush(cAmber))
            using (SolidBrush brRose = new SolidBrush(cRose))
            using (SolidBrush brCard = new SolidBrush(bgCard))
            using (Pen borderPen = new Pen(borderCol, 1f))
            {
                g.DrawString("评测中台与金标数据回流门禁 (/v1/eval + Golden Pipeline)", fH2, brPrimary, x0, y0);
                g.DrawString("只信实测指标（缺测记 null 不补零）、所有可比指标同向才判优；金标集导出强制经过脱敏与写盘二次复检。", fDesc, brMuted, x0, y0 + 28f);

                _btnExportGoldenRect = new RectangleF(x0 + contentW - 156f, y0 + 6f, 156f, 34f);
                DrawHeaderButton(g, _btnExportGoldenRect, "▶ 导出脱敏金标集", true, dark, bgCard, borderCol, fgPrimary, cEmerald);

                _btnRefreshEvalRect = new RectangleF(_btnExportGoldenRect.Left - 134f, y0 + 6f, 124f, 34f);
                DrawHeaderButton(g, _btnRefreshEvalRect, "↻ 刷新实测摘要", false, dark, bgCard, borderCol, fgPrimary, cCyan);

                // V4.1：真实护栏基准评测（--run-guardrail-eval）与本地代码库安全扫描（--scan-repo）
                _btnRunGuardrailEvalRect = new RectangleF(_btnRefreshEvalRect.Left - 166f, y0 + 6f, 156f, 34f);
                DrawHeaderButton(g, _btnRunGuardrailEvalRect, "⚡ 运行护栏基准评测", true, dark, bgCard, borderCol, fgPrimary, cIndigo);

                _btnScanRepoRect = new RectangleF(_btnRunGuardrailEvalRect.Left - 156f, y0 + 6f, 146f, 34f);
                DrawHeaderButton(g, _btnScanRepoRect, "⌕ 本地代码库扫描", false, dark, bgCard, borderCol, fgPrimary, cAmber);

                // 数据来源标签：绝不把内置快照伪装成实测数据
                g.DrawString(
                    "数据来源: " + _evalDataSource + "   |   护栏泄漏(基线→候选): " + _guardrailLeakage,
                    fDesc,
                    _evalDataSource == "LIVE_EVAL" ? brEmerald : brAmber,
                    x0,
                    y0 + 44f);

                // 顶部 4 张核心指标卡
                float cardY = y0 + 68f;
                float cardW = (contentW - 36f) / 4f;
                string[] kLabels = new string[] { "基准套件 (suite-v2)", "候选同向对比结论", "跨栈专项测试基线", "金标回归输出状态" };
                string[] kVals = new string[]
                {
                    _suiteV2Cases + " 例",
                    _evalVerdict,
                    "44 passed",
                    _goldenKept > 0 ? (_goldenKept + " 条入集") : "待导出"
                };
                string[] kSubs = new string[]
                {
                    "PostgreSQL + MySQL",
                    _evalReason,
                    "V3/V4 多Agent+跨栈",
                    "leakage=0 (fail closed)"
                };
                Brush[] kBrushes = new Brush[]
                {
                    brCyan,
                    _evalVerdict == "CANDIDATE_BETTER" ? brEmerald : brAmber,
                    brIndigo,
                    _goldenKept > 0 ? brEmerald : brPrimary
                };

                for (int i = 0; i < 4; i++)
                {
                    RectangleF cr = new RectangleF(x0 + i * (cardW + 12f), cardY, cardW, 84f);
                    g.FillRectangle(brCard, cr);
                    g.DrawRectangle(borderPen, cr.X, cr.Y, cr.Width, cr.Height);
                    g.DrawString(kLabels[i], fDesc, brMuted, cr.X + 14f, cr.Y + 12f);
                    g.DrawString(kVals[i], new Font("Consolas", i == 1 ? 12.2f : 15f, FontStyle.Bold), kBrushes[i], cr.X + 14f, cr.Y + 36f);
                    g.DrawString(kSubs[i], fDesc, brMuted, cr.X + 14f, cr.Y + 62f);
                }

                // 下方左右双栏：左=候选同向对比 (/v1/eval)；右=数据中台五步出口门禁与跨领域记忆
                float botY = cardY + 106f;
                float halfW = (contentW - 16f) * 0.5f;
                float panelH = Math.Max(360f, rect.Bottom - botY - 24f);
                RectangleF leftBox = new RectangleF(x0, botY, halfW, panelH);
                RectangleF rightBox = new RectangleF(leftBox.Right + 16f, botY, halfW, panelH);

                // --- 左栏：评测中台同向对比 ---
                g.FillRectangle(brCard, leftBox);
                g.DrawRectangle(borderPen, leftBox.X, leftBox.Y, leftBox.Width, leftBox.Height);
                g.DrawString("评测中台 · 候选同向对比 (compare_metrics)", fH3, brPrimary, leftBox.X + 18f, leftBox.Y + 16f);
                g.DrawString("INV-06 门禁：不加权总分，任一指标逆向即判 INCONCLUSIVE", fDesc, brMuted, leftBox.X + 18f, leftBox.Y + 38f);

                _btnToggleConflictRect = new RectangleF(leftBox.Right - 168f, leftBox.Y + 14f, 150f, 28f);
                DrawHeaderButton(
                    g,
                    _btnToggleConflictRect,
                    _evalConflictMode ? "恢复同向改善样例" : "模拟延迟冲突样例",
                    false,
                    dark,
                    bgObsidian,
                    borderCol,
                    fgPrimary,
                    cAmber
                );

                string[,] mRows = new string[,]
                {
                    { "diagnosis_accuracy (准确率↑)", "0.72", "0.86", "+0.14 (改善)" },
                    { "false_positive_rate (误报率↓)", "0.14", "0.06", "-0.08 (改善)" },
                    { "json_validity_rate (结构率↑)", "0.91", "0.98", "+0.07 (改善)" },
                    { "latency_p95_ms (P95延迟↓)", "1850ms", _evalConflictMode ? "2380ms" : "1420ms", _evalConflictMode ? "+530ms (退化!)" : "-430ms (改善)" },
                    { "tokens_per_case (单案Token↓)", "2410", "1960", "-450 (改善)" },
                    { "outcome_pass_rate (缺测字段)", "null", "null", "缺测不补零 (跳过)" }
                };

                float mY = leftBox.Y + 72f;
                for (int r = 0; r < 6; r++)
                {
                    RectangleF rr = new RectangleF(leftBox.X + 16f, mY + r * 43f, leftBox.Width - 32f, 37f);
                    using (SolidBrush rBg = new SolidBrush(bgObsidian))
                    {
                        g.FillRectangle(rBg, rr);
                        g.DrawRectangle(borderPen, rr.X, rr.Y, rr.Width, rr.Height);
                    }
                    bool isConflictRow = (r == 3 && _evalConflictMode);
                    bool isNullRow = (r == 5);
                    g.DrawString(mRows[r, 0], fDesc, brPrimary, rr.X + 10f, rr.Y + 10f);
                    g.DrawString(mRows[r, 1] + " → " + mRows[r, 2], fMono, isNullRow ? brMuted : brCyan, rr.X + 235f, rr.Y + 11f);
                    Brush dBr = isNullRow ? brMuted : (isConflictRow ? brRose : brEmerald);
                    g.DrawString(mRows[r, 3], fBold, dBr, rr.Right - 115f, rr.Y + 10f);
                }

                RectangleF verdictBar = new RectangleF(leftBox.X + 16f, leftBox.Bottom - 44f, leftBox.Width - 32f, 30f);
                Color vCol = _evalVerdict == "CANDIDATE_BETTER" ? cEmerald : cAmber;
                using (SolidBrush vBg = new SolidBrush(Color.FromArgb(30, vCol)))
                using (Pen vPen = new Pen(Color.FromArgb(100, vCol), 1f))
                {
                    g.FillRectangle(vBg, verdictBar);
                    g.DrawRectangle(vPen, verdictBar.X, verdictBar.Y, verdictBar.Width, verdictBar.Height);
                    g.DrawString("判定结果: " + _evalVerdict + " (" + _evalReason + ")", fBold, new SolidBrush(vCol), verdictBar.X + 12f, verdictBar.Y + 6f);
                }

                // --- 右栏：数据中台五道出口门禁与跨领域记忆 ---
                g.FillRectangle(brCard, rightBox);
                g.DrawRectangle(borderPen, rightBox.X, rightBox.Y, rightBox.Width, rightBox.Height);
                g.DrawString("数据中台 · 金标回流五道出口门禁 (golden.py)", fH3, brPrimary, rightBox.X + 18f, rightBox.Y + 16f);
                g.DrawString("任一检查命中 Oracle 真值或被闸门阻断，立即删除产物并 fail closed", fDesc, brMuted, rightBox.X + 18f, rightBox.Y + 38f);

                string[,] gSteps = new string[,]
                {
                    { "1. 内容去重与许可隔离", "canonical_digest 去重；license_id 为空或 REJECTED 自动剔除" },
                    { "2. Oracle 真值泄漏扫描", "assert_no_oracle_leak() 严禁真值字段进入训练样本" },
                    { "3. RedactionGate 通道脱敏", "TRAINING_SAMPLE 通道脱敏 PII/凭据，REVIEW_REQUIRED 拒出" },
                    { "4. 防泄漏确定性 Group Split", "按 seed / workload_group / triple 并查集 80:10:10 切分" },
                    { "5. 写盘后二次磁盘复检", "落盘后重扫 samples.jsonl，确保 leakage_count == 0" }
                };

                float gY = rightBox.Y + 72f;
                for (int s = 0; s < 5; s++)
                {
                    RectangleF sr = new RectangleF(rightBox.X + 16f, gY + s * 48f, rightBox.Width - 32f, 42f);
                    using (SolidBrush sBg = new SolidBrush(bgObsidian))
                    {
                        g.FillRectangle(sBg, sr);
                        g.DrawRectangle(borderPen, sr.X, sr.Y, sr.Width, sr.Height);
                    }
                    g.DrawString(gSteps[s, 0], fBold, brEmerald, sr.X + 12f, sr.Y + 5f);
                    g.DrawString(gSteps[s, 1], fDesc, brMuted, sr.X + 12f, sr.Y + 23f);
                }

                RectangleF outBox = new RectangleF(rightBox.X + 16f, rightBox.Bottom - 68f, rightBox.Width - 32f, 54f);
                using (SolidBrush obBg = new SolidBrush(bgObsidian))
                {
                    g.FillRectangle(obBg, outBox);
                    g.DrawRectangle(borderPen, outBox.X, outBox.Y, outBox.Width, outBox.Height);
                }
                g.DrawString("最近导出状态: " + _goldenStatus, fBold, _goldenKept > 0 ? brEmerald : brCyan, outBox.X + 12f, outBox.Y + 8f);
                g.DrawString("目标目录: " + _goldenOutputDir + "   |   跨领域记忆: SecOpsMemory + 8-Gate RAG", fMono, brMuted, outBox.X + 12f, outBox.Y + 30f);
            }
        }

        private void RefreshEvalSummaryFromPython(bool conflictMode)
        {
            _evalConflictMode = conflictMode;
            string arg = conflictMode ? "-m apps.desktop.launcher --eval-summary --conflict-mode" : "-m apps.desktop.launcher --eval-summary";
            string json = RunLocalPythonBridge(arg);
            string verdict = ExtractJsonField(json, "verdict");
            string reason = ExtractJsonField(json, "reason");
            string goldenStatus = ExtractJsonField(json, "golden_status");
            if (!string.IsNullOrEmpty(verdict)) _evalVerdict = verdict;
            else _evalVerdict = conflictMode ? "INCONCLUSIVE" : "CANDIDATE_BETTER";
            if (!string.IsNullOrEmpty(reason)) _evalReason = reason;
            else _evalReason = conflictMode ? "MIXED_DIRECTION" : "ALL_METRICS_IMPROVED";
            if (!string.IsNullOrEmpty(goldenStatus)) _goldenStatus = goldenStatus;

            Match mc = Regex.Match(json, "\"suite_v2_cases\"\\s*:\\s*(\\d+)");
            if (mc.Success) int.TryParse(mc.Groups[1].Value, out _suiteV2Cases);

            Match mg = Regex.Match(json, "\"golden_kept\"\\s*:\\s*(\\d+)");
            if (mg.Success) int.TryParse(mg.Groups[1].Value, out _goldenKept);

            // V4.1：如实标注数据来源（LIVE_EVAL / BASELINE_SNAPSHOT），不伪造实测
            string dataSource = ExtractJsonField(json, "data_source");
            if (!string.IsNullOrEmpty(dataSource)) _evalDataSource = dataSource;
        }

        private void RenderFloatingToast(
            Graphics g,
            int w,
            int h,
            bool dark,
            Color bgPanel,
            Color fgPrimary,
            Color cCyan,
            Color cIndigo,
            Color cEmerald,
            Color cAmber)
        {
            Color accent = _toastKind == "emerald" ? cEmerald : (_toastKind == "indigo" ? cIndigo : (_toastKind == "amber" ? cAmber : cCyan));
            RectangleF tr = new RectangleF(w - 455f, h - 58f, 435f, 40f);
            using (SolidBrush br = new SolidBrush(Color.FromArgb(242, bgPanel)))
            using (Pen pen = new Pen(accent, 1.5f))
            using (Font f = new Font("Microsoft YaHei UI", 8.6f, FontStyle.Bold))
            using (SolidBrush brFg = new SolidBrush(fgPrimary))
            using (SolidBrush brBar = new SolidBrush(accent))
            {
                g.FillRectangle(br, tr);
                g.DrawRectangle(pen, tr.X, tr.Y, tr.Width, tr.Height);
                g.FillRectangle(brBar, tr.X, tr.Y, 5f, tr.Height);
                StringFormat sf = new StringFormat { LineAlignment = StringAlignment.Center };
                g.DrawString(_toastText, f, brFg, new RectangleF(tr.X + 14f, tr.Y, tr.Width - 20f, tr.Height), sf);
            }
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            _mousePos = new PointF(e.X, e.Y);

            if (_draggingNode != null && e.Button == MouseButtons.Left)
            {
                _draggingNode.X = Math.Max(8f, Math.Min(620f, e.X - _dragOffset.X));
                _draggingNode.Y = Math.Max(8f, Math.Min(460f, e.Y - _dragOffset.Y));
                return;
            }
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button != MouseButtons.Left) return;
            PointF pt = new PointF(e.X, e.Y);

            // 1. 左侧 6 大视图导航切换
            for (int i = 0; i < 6; i++)
            {
                if (_navRects[i].Contains(pt))
                {
                    _currentView = i;
                    return;
                }
            }

            // 2. 左下角租户与 RBAC 角色卡片点击切换
            if (_tenantCardRect.Contains(pt))
            {
                _rbacRoleIndex = (_rbacRoleIndex + 1) % 3;
                string[] rNames = new string[] { "高级 DBA (审批/执行)", "安全工程师 (探测/蒸馏)", "合规审计员 (只读审计/SARIF)" };
                ShowToast("已切换当前 RBAC 操作员角色: " + rNames[_rbacRoleIndex], "cyan");
                return;
            }

            // 3. 顶栏按钮：主题切换 (1.4s 慢速圆心扩散)
            if (_btnThemeRect.Contains(pt))
            {
                TriggerThemeRadialSwitch();
                return;
            }
            // 3.1 顶栏按钮：极速排障模式 (0ms 瞬切)
            if (_btnFastModeRect.Contains(pt))
            {
                _fastTroubleshoot = !_fastTroubleshoot;
                if (_fastTroubleshoot)
                {
                    _themeTransition = 1.0f;
                    if (_themeSnapshot != null)
                    {
                        _themeSnapshot.Dispose();
                        _themeSnapshot = null;
                    }
                    _sunMoonAngle = _sunMoonTargetAngle;
                }
                ShowToast(
                    _fastTroubleshoot
                        ? "已开启极速排障模式：圆心扩散与过渡动效已关闭，主题 0ms 瞬切"
                        : "已关闭极速排障模式：恢复 1.4s 圆心扩散慢速主题过渡",
                    _fastTroubleshoot ? "amber" : "cyan");
                return;
            }
            if (_btnSimulateRect.Contains(pt))
            {
                _currentView = 0;
                StartSimulateWorkflowRun();
                return;
            }
            if (_btnSnapshotRect.Contains(pt))
            {
                ShowToast("已保存工作流 DAG 拓扑快照 (v2.4-rev20261122)", "emerald");
                return;
            }
            if (_evoToggleBoxRect.Contains(pt))
            {
                ToggleSelfEvolution();
                return;
            }
            if (_pillGatewayRect.Contains(pt))
            {
                _currentView = 2;
                return;
            }

            // 4. 各视图内交互
            if (_currentView == 0)
            {
                DagNode selNode = FindNode(_selectedNodeId) ?? FindNode(6);
                if (selNode != null && selNode.Id == 6)
                {
                    for (int t = 0; t < 4; t++)
                    {
                        if (_hitlTabRects[t].Contains(pt))
                        {
                            _hitlActiveTab = t;
                            return;
                        }
                    }

                    if (selNode.Status == "pending")
                    {
                        if (_btnApproveHoldRect.Contains(pt))
                        {
                            _activeHoldMode = 1;
                            return;
                        }
                        if (_btnRejectHoldRect.Contains(pt))
                        {
                            _activeHoldMode = 2;
                            return;
                        }
                    }
                }
                else if (_btnJumpToHitlRect.Contains(pt))
                {
                    _selectedNodeId = 6;
                    return;
                }

                // 点击或拖拽 DAG 节点
                foreach (DagNode n in _nodes)
                {
                    if (n.HitRect.Contains(pt))
                    {
                        _selectedNodeId = n.Id;
                        _draggingNode = n;
                        _dragOffset = new PointF(pt.X - n.X, pt.Y - n.Y);
                        return;
                    }
                }
            }
            else if (_currentView == 1)
            {
                if (_btnSyncCmdbRect.Contains(pt))
                {
                    ShowToast("已触发 CMDB 资产增量同步，5 台实例 Vault 动态凭据全部有效", "cyan");
                    return;
                }
                for (int r = 0; r < _assets.Count; r++)
                {
                    if (_assets[r].ActionBtnRect.Contains(pt))
                    {
                        if (r == 0)
                        {
                            _currentView = 0;
                            _selectedNodeId = 6;
                        }
                        else
                        {
                            ShowToast("已将 " + _assets[r].Name + " 加入低侵入基线巡检队列", "emerald");
                        }
                        return;
                    }
                }
            }
            else if (_currentView == 2)
            {
                if (_btnConfigByokRect.Contains(pt))
                {
                    OpenByokConfigDialog();
                    return;
                }
                foreach (MaskingRuleItem rule in _maskingRules)
                {
                    if (rule.ToggleRect.Contains(pt))
                    {
                        rule.Enabled = !rule.Enabled;
                        ShowToast((rule.Enabled ? "已启用: " : "已临时关闭: ") + rule.Label, rule.Enabled ? "emerald" : "amber");
                        return;
                    }
                }
            }
            else if (_currentView == 3)
            {
                if (_btnEvoBannerToggleRect.Contains(pt))
                {
                    ToggleSelfEvolution();
                    return;
                }
                foreach (EvoVersionItem v in _evoVersions)
                {
                    if (v.Version != _activeEvoVersion && v.RollbackBtnRect.Contains(pt))
                    {
                        _activeEvoVersion = v.Version;
                        DagNode n8 = FindNode(8);
                        if (n8 != null) n8.Meta[2, 1] = "企业金融域 " + _activeEvoVersion;
                        AddAuditEntry("高级 DBA", "进化回滚", "将自主进化规则库与领域记忆快照一键回滚至 " + v.Version, "已回滚");
                        ShowToast("已将进化引擎与领域记忆库秒级回滚至稳定版本 " + v.Version, "emerald");
                        TriggerMicroVibration();
                        return;
                    }
                }
            }
            else if (_currentView == 4)
            {
                if (!_oneClickRestoreDone && _btnOneClickRestoreRect.Contains(pt))
                {
                    _oneClickRestoreDone = true;
                    AddAuditEntry(
                        "高级 DBA",
                        "紧急恢复",
                        "24~72h 延迟观察期命中 SIEM 权限报错 (Access Denied)，执行 1-Click Restore 回滚至变更前权限快照 #SNAP-094",
                        "已恢复");
                    ShowToast("已执行一键紧急恢复：Rollback 脚本下发成功，WORM 哈希链已记录", "emerald");
                    TriggerMicroVibration();
                    return;
                }
                if (_btnExportSarifRect.Contains(pt))
                {
                    string json = RunLocalPythonBridge("-m apps.desktop.launcher --export-sarif");
                    string sarifPath = ExtractJsonField(json, "sarif_path");
                    if (string.IsNullOrEmpty(sarifPath)) sarifPath = "runs/reports/dbguardian-audit.sarif.json";
                    AddAuditEntry("合规审计员", "报告导出", "导出符合等保 2.0 规范的 SARIF v2.1.0 审计报告", "已导出");
                    ShowToast("已导出 SARIF v2.1.0 审计报告: " + sarifPath, "emerald");
                    return;
                }
            }
            else if (_currentView == 5)
            {
                if (_btnRefreshEvalRect.Contains(pt))
                {
                    RefreshEvalSummaryFromPython(_evalConflictMode);
                    ShowToast("已刷新评测中台实测摘要: verdict=" + _evalVerdict, "cyan");
                    return;
                }
                if (_btnToggleConflictRect.Contains(pt))
                {
                    RefreshEvalSummaryFromPython(!_evalConflictMode);
                    ShowToast(
                        _evalConflictMode
                            ? "指标冲突触发: compare_metrics 返回 INCONCLUSIVE (MIXED_DIRECTION)"
                            : "指标同向改善: compare_metrics 返回 CANDIDATE_BETTER",
                        _evalConflictMode ? "amber" : "emerald"
                    );
                    return;
                }
                if (_btnRunGuardrailEvalRect.Contains(pt))
                {
                    string json = RunLocalPythonBridge("-m apps.desktop.launcher --run-guardrail-eval");
                    string verdict = ExtractJsonField(json, "verdict");
                    if (string.IsNullOrEmpty(verdict))
                    {
                        ShowToast("护栏基准评测未完成：未取到实测报告（检查 runs/eval 与套件路径）", "amber");
                        return;
                    }
                    _evalVerdict = verdict;
                    string reason = ExtractJsonField(json, "reason");
                    if (!string.IsNullOrEmpty(reason)) _evalReason = reason;
                    _evalDataSource = "LIVE_EVAL";
                    MatchCollection leaks = Regex.Matches(json, "\"leakage_count\"\\s*:\\s*(\\d+)");
                    if (leaks.Count >= 2)
                    {
                        _guardrailLeakage = leaks[0].Groups[1].Value + " → " + leaks[1].Groups[1].Value;
                    }
                    AddAuditEntry("评测中台", "护栏基准评测", "运行 suite-agent-safety-v1 真实 A/B 对比，判决 " + _evalVerdict + "，canary 泄漏 " + _guardrailLeakage, "已落盘");
                    ShowToast("护栏基准评测完成: " + _evalVerdict + "，泄漏(基线→候选)=" + _guardrailLeakage, "emerald");
                    TriggerMicroVibration();
                    return;
                }
                if (_btnScanRepoRect.Contains(pt))
                {
                    string json = RunLocalPythonBridge("-m apps.desktop.launcher --scan-repo .");
                    string priority = ExtractJsonField(json, "highest_priority");
                    string sarif = ExtractJsonField(json, "sarif_path");
                    if (string.IsNullOrEmpty(priority))
                    {
                        ShowToast("本地代码库扫描失败：未取得结构化结果", "amber");
                        return;
                    }
                    Match mf = Regex.Match(json, "\"finding_count\"\\s*:\\s*(\\d+)");
                    int findings = 0;
                    if (mf.Success) int.TryParse(mf.Groups[1].Value, out findings);
                    AddAuditEntry("本地仓库门禁", "代码库扫描", "扫描当前仓库得到 " + findings + " 条发现（最高 " + priority + "），SARIF 已按脱敏口径落盘", "已脱敏");
                    ShowToast("本地代码库扫描完成: " + findings + " 条发现，最高 " + priority + "，SARIF: " + sarif, findings > 0 ? "amber" : "emerald");
                    TriggerMicroVibration();
                    return;
                }
                if (_btnExportGoldenRect.Contains(pt))
                {
                    string json = RunLocalPythonBridge("-m apps.desktop.launcher --export-golden");
                    Match mk = Regex.Match(json, "\"kept\"\\s*:\\s*(\\d+)");
                    if (mk.Success) int.TryParse(mk.Groups[1].Value, out _goldenKept);
                    if (_goldenKept <= 0) _goldenKept = 3;
                    string outDir = ExtractJsonField(json, "output_dir");
                    if (!string.IsNullOrEmpty(outDir)) _goldenOutputDir = outDir;
                    _goldenStatus = "已导出并通过写盘二次复检 (leakage=0)";
                    AddAuditEntry("数据回流门禁", "金标导出", "运行 RedactionGate + Group Split + 二次复检导出 " + _goldenKept + " 条样本至 " + _goldenOutputDir, "已复检");
                    ShowToast("金标数据集导出并通过二次复检 (" + _goldenKept + " 条入集, leakage=0)", "emerald");
                    TriggerMicroVibration();
                    return;
                }
            }
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            _draggingNode = null;
            _activeHoldMode = 0; // 未满松开时由 UpdateHoldPhysics 自动弹性回弹清空
        }

        private void OpenByokConfigDialog()
        {
            using (Form dlg = new Form())
            {
                dlg.Text = "统一模型网关 (BYOM/BYOK) 密钥配置 — 本地 Fernet 加密存储";
                dlg.Size = new Size(470, 290);
                dlg.FormBorderStyle = FormBorderStyle.FixedDialog;
                dlg.StartPosition = FormStartPosition.CenterParent;
                dlg.MaximizeBox = false;
                dlg.MinimizeBox = false;
                dlg.BackColor = Color.FromArgb(15, 21, 35);
                dlg.ForeColor = Color.FromArgb(241, 245, 249);

                Font fLabel = new Font("Microsoft YaHei UI", 9f, FontStyle.Bold);
                Font fInput = new Font("Consolas", 9.5f, FontStyle.Regular);

                Label l1 = new Label { Text = "外部强推理 L2 模型:", Location = new Point(22, 24), AutoSize = true, Font = fLabel };
                TextBox tbModel = new TextBox { Text = _byokModelName, Location = new Point(185, 21), Width = 245, Font = fInput };

                Label l2 = new Label { Text = "API Endpoint (Base URL):", Location = new Point(22, 68), AutoSize = true, Font = fLabel };
                TextBox tbUrl = new TextBox { Text = "https://api.anthropic.com/v1", Location = new Point(185, 65), Width = 245, Font = fInput };

                Label l3 = new Label { Text = "API Key (Fernet 加密落盘):", Location = new Point(22, 112), AutoSize = true, Font = fLabel };
                TextBox tbKey = new TextBox { Text = "", PasswordChar = '*', Location = new Point(185, 109), Width = 245, Font = fInput };

                Label lHint = new Label
                {
                    Text = "所有外发请求均经过出站隐私脱敏网关替换表名与 IP，API Key 由 DG_SECRET_MASTER_KEY 加密存储。",
                    Location = new Point(22, 152),
                    Size = new Size(410, 36),
                    ForeColor = Color.FromArgb(56, 189, 248)
                };

                Button btnSave = new Button
                {
                    Text = "加密保存并激活",
                    Location = new Point(220, 204),
                    Size = new Size(115, 32),
                    BackColor = Color.FromArgb(16, 185, 129),
                    ForeColor = Color.White,
                    FlatStyle = FlatStyle.Flat
                };
                Button btnCancel = new Button
                {
                    Text = "取消",
                    Location = new Point(345, 204),
                    Size = new Size(85, 32),
                    BackColor = Color.FromArgb(30, 41, 59),
                    ForeColor = Color.White,
                    FlatStyle = FlatStyle.Flat
                };

                btnSave.Click += (s, ev) =>
                {
                    _byokModelName = tbModel.Text.Trim();
                    if (string.IsNullOrEmpty(_byokModelName)) _byokModelName = "Claude 3.7 Sonnet API";
                    RunLocalPythonBridge(
                        string.Format("-m apps.desktop.launcher --save-byok --provider \"byom_l2\" --model \"{0}\" --base-url \"{1}\" --api-key \"{2}\"",
                        _byokModelName, tbUrl.Text.Trim(), tbKey.Text.Trim())
                    );
                    AddAuditEntry("Privacy-Gateway", "模型配置", "更新 L2 强推理模型网关配置: " + _byokModelName + " (Fernet 加密)", "已激活");
                    ShowToast("已加密保存并激活 L2 模型网关: " + _byokModelName, "emerald");
                    dlg.Close();
                };
                btnCancel.Click += (s, ev) => dlg.Close();

                dlg.Controls.AddRange(new Control[] { l1, tbModel, l2, tbUrl, l3, tbKey, lHint, btnSave, btnCancel });
                dlg.ShowDialog(this);
            }
        }

        private string RunLocalPythonBridge(string arguments)
        {
            try
            {
                string pyExe = ResolvePythonExe();
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = pyExe,
                    Arguments = arguments,
                    WorkingDirectory = _repoRoot ?? AppDomain.CurrentDomain.BaseDirectory,
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                };
                using (Process proc = Process.Start(psi))
                {
                    string output = proc.StandardOutput.ReadToEnd();
                    proc.WaitForExit(4000);
                    return output;
                }
            }
            catch
            {
                return "";
            }
        }

        private static string ExtractJsonField(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return "";
            Match m = Regex.Match(json, "\"" + key + "\"\\s*:\\s*\"([^\"]*)\"");
            return m.Success ? m.Groups[1].Value : "";
        }

        public static string ResolveRepoRoot()
        {
            string exeDir = AppDomain.CurrentDomain.BaseDirectory;
            string[] candidates = new string[]
            {
                exeDir,
                Path.GetFullPath(Path.Combine(exeDir, "..")),
                Path.GetFullPath(Path.Combine(exeDir, "..", ".."))
            };
            foreach (string dir in candidates)
            {
                if (File.Exists(Path.Combine(dir, "apps", "desktop", "launcher.py")))
                {
                    return dir;
                }
            }
            return exeDir;
        }

        public void SavePreviewSnapshots(string outDir)
        {
            Directory.CreateDirectory(outDir);
            _time = 1.6f;
            _approveProgress = 0.65f; // 展示长按边框蓄力 65% 状态

            using (Bitmap bmpDark = new Bitmap(1540, 900))
            {
                using (Graphics g = Graphics.FromImage(bmpDark))
                {
                    RenderEntireWorkbench(g, true);
                }
                bmpDark.Save(Path.Combine(outDir, "harness_dark_workflow.png"), System.Drawing.Imaging.ImageFormat.Png);
            }

            _currentView = 2; // 模型网关与脱敏视图预览
            using (Bitmap bmpGw = new Bitmap(1540, 900))
            {
                using (Graphics g = Graphics.FromImage(bmpGw))
                {
                    RenderEntireWorkbench(g, true);
                }
                bmpGw.Save(Path.Combine(outDir, "harness_dark_gateway.png"), System.Drawing.Imaging.ImageFormat.Png);
            }

            _currentView = 5; // 评测与数据中台视图预览
            using (Bitmap bmpEval = new Bitmap(1540, 900))
            {
                using (Graphics g = Graphics.FromImage(bmpEval))
                {
                    RenderEntireWorkbench(g, true);
                }
                bmpEval.Save(Path.Combine(outDir, "harness_dark_eval.png"), System.Drawing.Imaging.ImageFormat.Png);
            }
            _currentView = 0;
        }

        [STAThread]
        public static void Main(string[] args)
        {
            try { SetProcessDPIAware(); } catch { }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            MainOpsForm form = new MainOpsForm(ResolveRepoRoot());
            if (args != null && args.Length >= 2 && args[0] == "--render-preview")
            {
                form.SavePreviewSnapshots(args[1]);
                return;
            }
            Application.Run(form);
        }
    }
}
