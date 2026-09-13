#!/usr/bin/env python3
"""Part 2b: Populate remaining tasks into existing workbook"""
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def main(output_path: str) -> None:
    """Populate tasks (Part 2b: DATA/LLM/MULTI/PERF/DEVOPS/ERR/CONF/AGENT/FE/COMP). Saves to output_path."""
    wb=load_workbook(output_path)
    ws=wb["详细任务清单"]
    NF=Font(name="Arial",size=9);BF=Font(name="Arial",bold=True,size=9)
    RED=PatternFill("solid",fgColor="FFCDD2");YEL=PatternFill("solid",fgColor="FFF9C4")
    GRN=PatternFill("solid",fgColor="C8E6C9");DIM=PatternFill("solid",fgColor="F0F2F5")
    P0F=PatternFill("solid",fgColor="E53935");P1F=PatternFill("solid",fgColor="FB8C00")
    P2F=PatternFill("solid",fgColor="FDD835")
    TB=Border(left=Side("thin","FFD0D0D0"),right=Side("thin","FFD0D0D0"),top=Side("thin","FFD0D0D0"),bottom=Side("thin","FFD0D0D0"))
    CA=Alignment(horizontal="center",vertical="center",wrap_text=True)
    LA=Alignment(horizontal="left",vertical="center",wrap_text=True)

    T=[
    # ═══ DATA ═══
    ("数据管理","DATA-01","数据库读写分离与连接池优化","68","90","无读写分离,连接池未调优","SQLAlchemy读写分离→read replica→连接池调优→慢查询分析","SQLAlchemy read/write splitting+pgbouncer","8","P1","无","读写分离生效,读请求分发到replica"),
    ("数据管理","DATA-02","数据库迁移CI集成与回滚方案","68","90","Alembic手动执行,无回滚验证","CI自动迁移→备份→测试验证→回滚脚本→蓝绿部署","Alembic CI+migration test+rollback","10","P1","无","迁移CI自动执行,有回滚方案"),
    ("数据管理","DATA-03","缓存层引入(Redis Cache-Aside)","68","90","无任何缓存策略","Cache-Aside→热点缓存→TTL→布隆过滤器→随机过期","Redis Cache+cache invalidation","12","P1","无","热点缓存命中率>80%"),
    ("数据管理","DATA-04","向量数据库专业化(Milvus/Qdrant)","68","90","嵌入存SQLite,无专用向量DB","评估Milvus/Qdrant→HNSW索引→混合搜索→向量+标量过滤","Milvus/Qdrant+pgvector备选+HNSW","15","P2","LLM-04","向量搜索P99<100ms"),
    ("数据管理","DATA-05","数据归档与冷热分离策略","68","90","全量在线,无归档","冷数据识别→S3/MinIO归档→在线压缩→分区表","S3归档+PostgreSQL分区+定时任务","12","P2","DATA-01","冷数据自动归档"),
    ("数据管理","DATA-06","备份恢复与灾备方案","68","90","无自动化备份","定时全量/增量→WAL归档→异地S3→定期演练→RPO<1h/RTO<4h","pg_dump+WAL archiving+S3","10","P1","无","RPO<1h,RTO<4h"),
    ("数据管理","DATA-07","数据变更审计与版本追踪","68","90","无数据变更历史","审计日志表→操作人/时间/IP→变更快照→版本回滚","Audit log triggers+数据版本表","8","P2","无","关键数据变更可追溯"),

    # ═══ LLM/AI ═══
    ("LLM集成","LLM-01","LLM Provider抽象层与多模型路由","78","95","仅支持OpenAI兼容API","Provider抽象接口→多模型路由→Fallback→成本/质量策略","Provider Registry+Model Router+Fallback","10","P2","无","多Provider多模型可路由"),
    ("LLM集成","LLM-02","Prompt版本管理与A/B实验","78","95","Prompt硬编码(agent_prompts.py 81KB)","Prompt外部化→Git版本管理→A/B测试→效果评估→灰度","Prompt版本管理+A/B test+效果评估","12","P2","LLM-07","Prompt变更可追踪,A/B实验量化"),
    ("LLM集成","LLM-03","LLM输出质量评估体系","78","95","无自动化质量评估","多维评估(准确性/相关性/安全性/格式)→自动评分→回归→标注集","LLM-as-a-Judge+RAGAS+人工标注","12","P2","LLM-02","LLM输出有量化质量指标"),
    ("LLM集成","LLM-04","嵌入模型管理与缓存优化","78","95","嵌入模型单一,无缓存","嵌入缓存→批量计算→模型选择→维度适配","嵌入缓存+批量API+模型选择器","6","P2","DATA-04","嵌入计算有缓存复用"),
    ("LLM集成","LLM-05","流式响应与断流恢复优化","78","95","流式基础可用但缺用户体验优化","SSE→渐进式UI→Token级渲染→中断恢复→错误处理","SSE+前端增量渲染+断流恢复","8","P2","无","流式稳定,中断可恢复"),
    ("LLM集成","LLM-06","Token预算与成本控制体系","78","95","Token计数不精确,无预算","精确计数→会话/用户/租户预算→超额策略→成本面板","tiktoken+预算管理器+成本Dashboard","8","P1","OBS-07","Token使用可追踪可限制"),

    # ═══ MULTI-TENANCY ═══
    ("多租户","MULTI-01","租户数据隔离加固(RLS)","55","95","SQLite为主,租户隔离不完整","tenant_id强制过滤→PostgreSQL RLS→tenant-aware ORM→隔离测试","PostgreSQL RLS+tenant-aware SQLAlchemy","12","P0","SEC-10","零跨租户泄露"),
    ("多租户","MULTI-02","租户级资源配额","55","95","无租户资源限制","CPU/内存/存储/API配额→超额策略→配额面板→通知","Quota management+超额策略","10","P0","MULTI-01","租户有明确配额"),
    ("多租户","MULTI-03","租户级并发与资源隔离","55","95","所有租户共享资源池","并发限制→优先级队列→资源预留→QoS保障","Redis Queue per tenant+priority","8","P0","MULTI-02","租户间资源不干扰"),
    ("多租户","MULTI-04","租户配置与定制化","55","95","租户无独立配置","租户Settings→功能开关→主题定制→通知偏好","Tenant settings table+feature flags","8","P1","MULTI-01","租户独立配置"),
    ("多租户","MULTI-05","租户使用统计与计费基础","55","95","无使用量和计费","资源用量→计费项→账单→趋势→结算","Metered billing+usage statistics","12","P2","MULTI-02","使用量可查询可导出"),
    ("多租户","MULTI-06","租户数据导入导出与迁移","55","95","无数据迁移能力","导出JSON/CSV→导入验证→跨租户迁移→完整性校验","Data export/import API+validation","10","P2","MULTI-01","租户数据完整导出导入"),
    ("多租户","MULTI-07","租户生命周期管理","55","95","无创建/暂停/删除流程","Provisioning→暂停/恢复→数据保留→删除→通知→审计","Tenant lifecycle state machine","8","P1","MULTI-01","租户全生命周期管理"),
    ("多租户","MULTI-08","沙箱资源租户隔离","55","95","Docker沙箱无租户隔离","租户沙箱池→资源上限→网络隔离→超时→清理","Per-tenant sandbox pools+limits","10","P1","MULTI-03","沙箱按租户隔离"),
    ("多租户","MULTI-09","跨租户安全审计","55","95","无跨租户访问监控","审计日志→异常跨租户访问→定期报告→自动扫描","Audit log+anomaly detection+report","6","P1","SEC-10","异常跨租户访问告警"),
    ("多租户","MULTI-10","租户Agent/工具/技能隔离","55","95","Agent和工具全局共享","租户Agent注册→工具清单隔离→技能权限→模型配额","Tenant-scoped registries+permission","8","P1","MULTI-01","Agent/工具/技能按租户隔离"),

    # ═══ PERFORMANCE ═══
    ("性能扩展","PERF-01","水平扩展架构设计","52","90","无状态设计不完整","无状态化→Redis Session→多实例→负载均衡→K8s HPA","Stateless+Redis Session+K8s HPA","10","P1","无","3+实例水平扩展"),
    ("性能扩展","PERF-02","多级缓存策略全面实施","52","90","无缓存层","L1本地+L2 Redis→查询缓存→页面缓存→CDN","Multi-level cache+Redis+CDN","10","P1","DATA-03","缓存命中率>80%"),
    ("性能扩展","PERF-03","数据库查询优化","52","90","部分N+1,无查询分析","慢查询→N+1消除→索引优化→查询重写→分页","SQLAlchemy eager loading+query opt","8","P1","无","慢查询消除"),
    ("性能扩展","PERF-04","异步任务队列优化(Priority/DLQ)","52","90","基础Redis队列,无优先级/死信","优先级→延迟→死信→重试→进度","Redis Streams/Celery+任务面板","10","P2","无","任务有优先级/重试/进度"),
    ("性能扩展","PERF-05","连接池与资源池调优","52","90","连接池默认参数","DB连接池→Redis连接池→HTTP复用→泄漏检测","SQLAlchemy pool+httpx pool config","5","P1","无","连接池合理无泄漏"),
    ("性能扩展","PERF-06","前端性能优化(Bundle/Lazy/Lighthouse)","52","90","无代码分割/懒加载","Code Split→Lazy→Tree Shaking→Image→Bundle分析","React.lazy+Vite优化+Lighthouse","10","P1","无","Lighthouse性能>90"),
    ("性能扩展","PERF-07","静态资源CDN加速","52","90","静态资源由应用服务器提供","CDN→资源指纹→缓存策略→Brotli压缩→预加载","CDN+资源hash+Brotli","5","P2","PERF-06","首屏加载<2s"),
    ("性能扩展","PERF-08","数据库索引策略优化","52","90","缺少索引策略","索引分析→缺失索引→冗余索引→使用统计→维护","Index advisor+pg_stat_statements","5","P1","PERF-03","关键查询有合适索引"),
    ("性能扩展","PERF-09","API响应压缩与优化","52","90","API未压缩","Gzip/Brotli→响应精简→字段筛选→ETag","FastAPI GZipMiddleware+ETag","3","P2","API-03","API响应压缩启用"),
    ("性能扩展","PERF-10","弹性伸缩与自动扩缩容","52","90","无自动扩缩容","HPA→VPA推荐→Cluster Autoscaler→预热→保护","K8s HPA/VPA+custom metrics","8","P1","PERF-01","负载自动扩缩容"),
    ("性能扩展","PERF-11","数据库分片与分区策略","52","90","单DB无分区分片","表分区(时间/租户)→分片评估→读写分离→垂直拆分","PostgreSQL partition+pg_partman","12","P2","PERF-03","大表分区性能稳定"),

    # ═══ DEVOPS ═══
    ("DevOps","DEVOPS-01","CI/CD流水线(GitHub Actions)","38","95","完全无CI/CD","构建→测试→扫描→镜像→部署→冒烟","GitHub Actions+Docker+K8s","10","P0","无","代码提交自动构建测试部署"),
    ("DevOps","DEVOPS-02","GitOps部署(ArgoCD/Flux)","38","95","手动部署","Git作为单一事实源→ArgoCD→自动同步→回滚","ArgoCD/Flux+Git仓库","10","P0","DEVOPS-01","GitOps全自动部署回滚"),
    ("DevOps","DEVOPS-03","容器化与Kubernetes编排","38","95","基础Docker,无K8s编排","Dockerfile最佳实践→K8s manifests→Helm Chart→Health Probes","Docker+K8s+Helm+Probes","12","P0","DEVOPS-01","生产K8s集群运行"),
    ("DevOps","DEVOPS-04","基础设施即代码(Terraform)","38","95","无IaC","Terraform→云资源定义→状态管理→模块化→CI集成","Terraform+Remote State","15","P1","DEVOPS-03","基础设施版本化可复现"),
    ("DevOps","DEVOPS-05","环境管理(Dev/Staging/Prod)","38","95","环境切换混乱","环境隔离→配置分离→数据隔离→访问控制→Promotion流程","Environment management+config分离","8","P1","DEVOPS-03","环境独立可切换"),
    ("DevOps","DEVOPS-06","灰度发布与金丝雀部署","38","95","无灰度发布","Argo Rollouts→金丝雀→蓝绿→流量分割→自动回滚","Argo Rollouts+Istio/Ingress","12","P1","DEVOPS-02","灰度验证后全量"),
    ("DevOps","DEVOPS-07","密钥与配置管理(SealedSecrets)","38","95","密钥管理混乱","SealedSecrets/Vault→Secret加密→注入→轮换→审计","SealedSecrets+HashiCorp Vault","8","P1","DEVOPS-03","Secret安全存储和轮换"),
    ("DevOps","DEVOPS-08","构建缓存与加速","38","95","每次全量构建","Docker layer cache→依赖缓存→并行构建→registry mirror","BuildKit+Docker cache+registry","5","P2","DEVOPS-01","构建时间<5min"),
    ("DevOps","DEVOPS-09","日志收集与聚合(EFK/Loki)","38","95","日志分散各容器","Filebeat/Fluentd→ES/Loki→Kibana/Grafana→查询","EFK Stack/Loki+Grafana","8","P1","OBS-01","日志集中可查询"),
    ("DevOps","DEVOPS-10","灾难恢复演练与Runbook","38","95","无灾备演练","Runbook→恢复步骤→演练计划→演练记录→改进","DR Runbook+定期演练","8","P1","DEVOPS-03","灾后30min内恢复"),
    ("DevOps","DEVOPS-11","开发者体验(DX)优化","38","95","本地开发环境搭建复杂","Dev Container→docker-compose一键启动→Mock服务→热重载","Dev Container+docker-compose+mock","8","P2","DEVOPS-03","新人5min启动开发"),

    # ═══ ERROR HANDLING ═══
    ("错误处理","ERR-01","全局异常处理器","48","90","无自定义异常处理器","@app.exception_handler→异常映射→标准响应→日志→trace_id","FastAPI exception handlers+标准化","5","P1","OBS-01","所有异常统一格式响应"),
    ("错误处理","ERR-02","熔断器模式(Circuit Breaker)","48","90","无熔断保护","熔断器→半开状态→自动恢复→监控→LLM调用/DB/下游","pybreaker/aiobreaker+监控","10","P1","OBS-02","下游故障自动熔断恢复"),
    ("错误处理","ERR-03","重试策略标准化(指数退避)","48","90","重试逻辑各有实现","统一重试装饰器→指数退避→jitter→max_retries→不可重试错误","tenacity+统一装饰器","8","P1","ERR-01","重试行为一致可预测"),
    ("错误处理","ERR-04","优雅降级与功能开关","48","90","功能故障无降级","功能开关→降级策略→静态降级→运行时切换→监控","Feature flags+graceful degradation","10","P1","ERR-02","功能故障优雅降级"),
    ("错误处理","ERR-05","请求超时与取消传播","48","90","超时配置不统一","统一超时→context取消→asyncio.timeout→propagation","asyncio.timeout+context传播","5","P1","无","超时行为统一"),
    ("错误处理","ERR-06","数据验证失败友好反馈","48","90","错误信息不友好","Pydantic error→友好消息→字段级提示→i18n错误信息","Pydantic error handler+友好映射","5","P2","ERR-01","用户看到友好错误提示"),
    ("错误处理","ERR-07","资源泄漏防护与清理","48","90","无资源泄漏检测","DB连接/文件句柄/HTTP连接→context manager→检测→告警","context manager+资源追踪","5","P1","无","零资源泄漏"),
    ("错误处理","ERR-08","死信队列与失败任务处理","48","90","失败任务无隔离","DLQ→失败任务存储→人工审查→重放→根因分析","Redis Streams DLQ+管理面板","8","P1","PERF-04","失败任务可追溯可重放"),
    ("错误处理","ERR-09","用户操作撤销与补偿机制","48","90","操作无撤销","事务补偿→Saga模式→undo操作→操作日志","Saga pattern+compensation","12","P2","ERR-04","关键操作可撤销补偿"),

    # ═══ CONFIG ═══
    ("配置管理","CONF-01","配置中心化(Consul/Etcd)","68","85","环境变量+pydantic Settings","分布式配置中心→动态刷新→版本管理→配置审计","Consul/Etcd+动态配置","10","P2","无","配置集中管理,动态刷新"),
    ("配置管理","CONF-02","配置验证与Schema定义","68","85","部分配置无验证","完整Schema→启动验证→运行时校验→错误提示","Pydantic v2 Settings+strict validation","5","P2","CONF-01","配置错误启动时发现"),
    ("配置管理","CONF-03","多环境配置分离","68","85","环境配置方式不统一","config/{dev,staging,prod}.yaml→继承→敏感信息外部注入","YAML config+pydantic-settings","8","P2","无","多环境配置清晰隔离"),
    ("配置管理","CONF-04","功能开关(Feature Flags)","68","85","无功能开关机制","Feature flag→分阶段→百分比→user→kill switch","LaunchDarkly/自建feature flag","12","P2","ERR-04","功能可灰度发布紧急关闭"),
    ("配置管理","CONF-05","配置变更通知与审计","68","85","配置变更无通知","变更日志→通知→回滚→审批流程→diff","Config change audit+notification","5","P2","CONF-01","配置变更有审计可回滚"),

    # ═══ AGENT/SKILL ═══
    ("Agent体系","AGENT-01","Agent编排工作流引擎","76","95","FSM状态机可用但编排能力有限","DAG工作流→条件分支→并行→子Agent调用→可视化编排","LangGraph/自研工作流引擎","15","P2","无","复杂多步骤Agent流程可编排"),
    ("Agent体系","AGENT-02","Agent评测与基准体系","76","95","无Agent效果评测","评测数据集→多维指标(准确/效率/安全/成本)→排行榜→回归","Agent eval framework+benchmark","12","P2","LLM-03","Agent有量化评测指标"),
    ("Agent体系","AGENT-03","多Agent协作与群集(Swarm)","76","95","swarm模块基础,协作能力弱","Agent间通信协议→任务分配→结果聚合→冲突解决","Swarm protocol+task distribution","15","P2","AGENT-01","多Agent协作任务完成"),
    ("Agent体系","AGENT-04","Skill市场与热加载","76","95","技能系统有但缺市场/发现","Skill marketplace→搜索→安装→评分→版本→沙箱安全","Skill registry+marketplace+security","10","P2","无","Skill可发现可安装可评分"),
    ("Agent体系","AGENT-05","Agent记忆长期化与知识图谱","76","95","记忆有基础但缺知识图谱","KG构建→实体关系→推理→记忆演化→知识蒸馏","Neo4j/自建KG+记忆演化","15","P2","LLM-04","Agent知识持续积累推理"),
    ("Agent体系","AGENT-06","工具调用可靠性增强","76","95","工具可靠性有基础框架","工具超时→重试分类→降级策略→工具健康检查→熔断","Tool health check+reliability分类","8","P1","ERR-03","工具调用99.9%成功率"),
    ("Agent体系","AGENT-07","人机协同(Human-in-the-Loop)","76","95","HITL机制不完善","审批节点→人工介入→反馈循环→权限控制→审计","HITL workflow+审批+权限","12","P2","AGENT-01","关键操作人工可介入审批"),

    # ═══ FRONTEND ═══
    ("前端体验","FE-01","响应式设计与移动端适配","58","90","桌面端为主,移动端体验差","Responsive breakpoints→Mobile-first→触摸交互→自适应布局","Tailwind responsive+Mobile-first","12","P1","无","移动端体验良好"),
    ("前端体验","FE-02","国际化i18n(中文/英文/日文)","58","90","仅中文,无i18n","i18n框架→翻译管理→语言检测→日期/货币→RTL","react-i18next/next-intl+Crowdin","15","P1","无","至少3语言支持"),
    ("前端体验","FE-03","无障碍性(a11y/WCAG 2.1 AA)","58","90","无可访问性","语义HTML→ARIA→键盘导航→色对比→屏幕阅读器→axe","WCAG 2.1 AA+axe-core+键盘","12","P1","FE-01","WCAG 2.1 AA认证"),
    ("前端体验","FE-04","离线能力与PWA","58","90","无离线支持","Service Worker→离线缓存→PWA manifest→推送通知→安装","Workbox+PWA+IndexedDB","10","P2","FE-03","离线可基本使用"),
    ("前端体验","FE-05","加载状态与骨架屏","58","90","加载状态不统一","Skeleton Screen→Loading→Empty→Error→Success统一","Suspense+Skeleton+ErrorBoundary","5","P2","无","加载状态体验一致"),
    ("前端体验","FE-06","前端错误边界与降级","58","90","无前端错误边界","React ErrorBoundary→组件级降级→错误上报→重试","ErrorBoundary+降级UI+Sentry","5","P2","OBS-06","组件错误不影响全局"),
    ("前端体验","FE-07","主题与品牌定制","58","90","主题能力有限","主题系统→CSS变量→暗黑模式→品牌定制→用户偏好","CSS variables+theme context+dark","8","P2","MULTI-04","多主题/暗黑/品牌定制"),
    ("前端体验","FE-08","微前端架构评估","58","90","巨石前端应用","Module Federation→独立部署→团队自治→共享组件库","Module Federation/qiankun","15","P2","无","模块独立开发部署"),
    ("前端体验","FE-09","虚拟滚动与大列表性能","58","90","大列表卡顿","Virtual Scroll→窗口化→无限加载→item回收","react-window/react-virtuoso","5","P2","PERF-06","万级列表流畅滚动"),
    ("前端体验","FE-10","前端自动化测试(Cypress/Testing Library)","58","90","前端测试不足","组件测试→集成测试→视觉回归→a11y测试","Testing Library+Cypress+Percy","10","P1","TEST-02","组件和交互有测试覆盖"),

    # ═══ COMPLIANCE ═══
    ("合规治理","COMP-01","完整审计日志系统","42","95","基础审计,不完整","CRUD审计→敏感操作→操作链→导出→保留→防篡改","Audit log service+防篡改存储","12","P0","OBS-01","全操作审计可追溯"),
    ("合规治理","COMP-02","数据隐私与GDPR合规","42","95","无GDPR合规框架","数据地图→同意管理→DSAR→数据最小化→DPIA","GDPR合规框架+DSAR工具","15","P0","SEC-14","GDPR合规就绪"),
    ("合规治理","COMP-03","审批工作流引擎","42","95","基础governance模块,不完整","多级审批→并行/串行→条件→超时→委托→通知","Workflow engine+审批配置","12","P0","无","关键操作有审批"),
    ("合规治理","COMP-04","数据分类与标记","42","95","无数据分类","敏感数据自动发现→分类→标签→DLP策略→报告","Data classification+标签系统","10","P1","COMP-01","敏感数据有分类标签"),
    ("合规治理","COMP-05","安全事件响应流程","42","95","无安全事件响应","事件分类→响应SOP→升级→沟通→事后分析→改进","Security IR plan+演练","8","P1","SEC-08","安全事件有标准响应"),
    ("合规治理","COMP-06","Soc2/ISO27001合规准备","42","95","无合规认证准备","差距分析→政策文档→控制实施→证据收集→认证审计","SOC2/ISO27001合规框架","20","P2","COMP-01~05","合规认证就绪"),
    ("合规治理","COMP-07","模型负责任AI与偏见检测","42","95","无AI公平性/安全性","偏见检测→毒性过滤→公平性指标→可解释性→安全护栏","AI Safety+Fairness+Guardrails","12","P1","LLM-03","AI输出有安全护栏"),
    ("合规治理","COMP-08","数据主权与区域部署","42","95","无区域数据控制","数据驻留→区域部署→跨区域传输控制→合规认证","Multi-region deployment+数据本地化","15","P2","COMP-02","数据主权合规"),
    ("合规治理","COMP-09","成本管理与FinOps","42","95","基础成本服务,不够完善","资源成本→LLM成本→预算→预测→优化建议→showback","FinOps+cost allocation+optimization","10","P2","LLM-06","成本可分配可优化"),
    ("合规治理","COMP-10","业务连续性计划(BCP)","42","95","无BCP","BCP文档→关键功能→RTO/RPO→恢复策略→演练","BCP plan+定期桌面演练","10","P2","DEVOPS-10","BCP文档完备"),
    ("合规治理","COMP-11","第三方依赖安全与供应链","42","95","无供应链安全","SBOM→依赖审计→许可证合规→漏洞监控→自动更新","SBOM+依赖扫描+许可证检查","8","P1","SEC-08","依赖安全可控"),
    ]

    start_row=55  # Continue from where part2a left off
    for i,(dim,id,title,sc,tg,desc,act,tech,days,pri,dep,acc) in enumerate(T):
        r=start_row+i;gap=int(tg)-int(sc)
        vals=[dim,id,title,int(sc),int(tg),gap,desc,act,tech,int(days),pri,dep,acc]
        for j,v in enumerate(vals,1):
            c=ws.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
            if i%2==0:c.fill=DIM
        ws.cell(row=r,column=4).alignment=CA;ws.cell(row=r,column=5).alignment=CA
        ws.cell(row=r,column=6).alignment=CA;ws.cell(row=r,column=10).alignment=CA
        ws.cell(row=r,column=11).alignment=CA;ws.cell(row=r,column=12).alignment=CA
        sc_c=ws.cell(row=r,column=4)
        if int(sc)<50:sc_c.fill=RED;sc_c.font=Font(name="Arial",bold=True,size=10,color="FFC62828")
        elif int(sc)<70:sc_c.fill=YEL;sc_c.font=Font(name="Arial",bold=True,size=10,color="FFE65100")
        else:sc_c.fill=GRN;sc_c.font=Font(name="Arial",bold=True,size=10,color="FF2E7D32")
        gap_c=ws.cell(row=r,column=6);gap_c.font=Font(name="Arial",bold=True,size=9,color="FFC62828")
        pri_c=ws.cell(row=r,column=11)
        if "P0" in pri:pri_c.fill=P0F;pri_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        elif "P1" in pri:pri_c.fill=P1F;pri_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        else:pri_c.fill=P2F;pri_c.font=Font(name="Arial",bold=True,size=9)
        ws.cell(row=r,column=14,value=0);ws.cell(row=r,column=14).alignment=CA;ws.cell(row=r,column=14).font=NF
        ws.cell(row=r,column=16,value="未开始");ws.cell(row=r,column=16).alignment=CA;ws.cell(row=r,column=16).font=NF

    wb.save(output_path)
    print(f"Part 2b done: {len(T)} tasks written from row {start_row} to {start_row+len(T)-1}")
    print(f"Total tasks in task list: {start_row+len(T)-2}")



# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import os as _os

    _default = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")
    _parser = argparse.ArgumentParser(description="Populate tasks (Part 2b: DATA/LLM/MULTI/PERF/DEVOPS/ERR/CONF/AGENT/FE/COMP)")
    _parser.add_argument("--output", "-o", default=_default, help=f"Output xlsx path (default: {{_default}})")
    _args = _parser.parse_args()
    main(_args.output)
