#!/usr/bin/env python3
"""Part 2a: Populate tasks (first half) into existing workbook"""
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def main(output_path: str) -> None:
    """Populate tasks (Part 2a: ARCH/API/SEC/TEST/OBS). Saves to output_path."""
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
    # (dim, id, title, score, target, desc, action, tech, days, pri, dep, accept)
    # ═══ ARCH ═══
    ("架构设计","ARCH-01","引入DDD分层架构(Domain/Application/Infra/Interface)","72","95","services目录247文件扁平化，缺乏分层边界","定义四层架构→识别聚合根→抽取领域服务→迁移代码→引入DI","Python Protocol + dependency-injector","20","P1","无","目录分层清晰，核心业务逻辑无框架依赖"),
    ("架构设计","ARCH-02","统一编码规范(MyPy/Ruff/pre-commit)","72","95","无代码格式化、类型检查、pre-commit钩子","Ruff+MyPy→pre-commit→.editorconfig→CI门禁","Ruff(替代Flake8+isort)+MyPy strict+pre-commit","5","P1","无","CI类型检查通过率100%"),
    ("架构设计","ARCH-03","消除循环依赖与清理废弃代码","72","95","main.py sys.path.insert模式，模块交叉import","import-linter检测→vulture死代码→删除废弃引用","import-linter + vulture","8","P2","ARCH-01","零循环依赖"),
    ("架构设计","ARCH-04","核心服务接口抽象与依赖注入","72","95","服务间直接调用具体类，无替换性","Protocol/ABC接口→DI容器→可测试架构","Python Protocol + dependency-injector","12","P2","ARCH-01","核心服务可mock独立测试"),
    ("架构设计","ARCH-05","模块包结构标准化与公共API","72","95","247文件扁平，__init__未暴露公共API","按有界上下文重组→__init__暴露API→模块README","Python包结构最佳实践","10","P2","ARCH-01","导入路径清晰，模块API明确"),
    ("架构设计","ARCH-06","引入事件驱动架构(Redis Streams)","72","95","同步调用链长，服务紧耦合","定义领域事件→Redis Streams→异步处理器","Redis Streams + asyncio EventBus","15","P2","ARCH-01","核心事件异步解耦"),
    ("架构设计","ARCH-07","统一异步编程规范","72","95","同步/异步混用，阻塞事件循环","审计同步调用→替换async→线程池CPU任务","asyncio.to_thread + anyio","10","P1","ARCH-02","所有IO异步"),
    ("架构设计","ARCH-08","代码复杂度治理","72","95","agent_prompts.py 81.8KB，函数过长","radon检测→拆分超大函数/文件→CI门禁","radon + wily复杂度追踪","8","P2","ARCH-02","圈复杂度<15，单文件<500行"),

    # ═══ API ═══
    ("API设计","API-01","集成OpenAPI完整文档与示例","62","95","FastAPI默认OpenAPI无描述/无示例","response_model→Field(description/example)→tags→自定义Swagger","FastAPI OpenAPI + Pydantic Field注解","8","P1","无","Swagger可交互测试"),
    ("API设计","API-02","API版本化策略(/api/v1/)","62","95","统一/api前缀无版本号","引入/api/v1/→兼容策略→Header版本协商→弃用通知","URL路径版本+Accept-Version header","8","P1","无","多版本共存"),
    ("API设计","API-03","统一响应格式与分页规范","62","95","分页不一致，响应格式各异","BaseResponse[T,meta,pagination]→Cursor/Offset→统一错误格式","Pydantic泛型BaseResponse[T] + 分页协议","5","P1","无","所有API响应统一"),
    ("API设计","API-04","请求验证强化(Pydantic Models)","62","95","entity接受dict无schema验证","所有endpoint定义Pydantic模型→max_length→正则验证","Pydantic v2 validators + Annotated","8","P1","API-03","严格schema验证"),
    ("API设计","API-05","批量操作API","62","95","无批量增删改端点","bulk endpoint→部分成功→事务保护","POST /api/v1/{entity}/bulk","6","P2","API-03","批量操作每项结果独立"),
    ("API设计","API-06","TypeScript SDK自动生成与发布","62","95","前端手写HTTP调用","OpenAPI Generator→npm包→CI自动生成→版本同步","openapi-generator-cli + npm publish","8","P2","API-01","TS SDK与API同步"),
    ("API设计","API-07","Webhook事件通知机制","62","95","外部系统无法订阅事件","注册API→事件触发→重试→HMAC签名验证→交付日志","Webhook注册表+异步队列+签名","12","P2","ARCH-06","Webhook注册/触发/重试"),
    ("API设计","API-08","GraphQL/BFF层评估与实现","62","95","前端多次调用获取关联数据","评估场景→Strawberry→字段选择→关联加载→N+1优化","Strawberry-GraphQL + DataLoader","15","P2","API-05","关键页面减少50%请求"),
    ("API设计","API-09","HATEOAS API可发现性","62","95","响应无链接导航","_links字段→根入口资源地图→rel描述","HAL风格_links","5","P2","API-03","客户端可发现资源"),
    ("API设计","API-10","API使用分析与监控面板","62","95","无API调用量统计","Middleware记录→TimescaleDB→Grafana面板→异常检测","Middleware+TimescaleDB+Grafana","10","P2","OBS-03","管理员查看统计趋势"),

    # ═══ SECURITY ═══
    ("安全认证","SEC-01","生产级JWT密钥管理(移除dev-secret默认值)","48","95","JWT_SECRET='dev-secret-change-me'，无密钥轮换","移除默认值→Vault获取→密钥轮换→Refresh Token","HashiCorp Vault+Refresh Token+在线轮换","8","P0","无","生产强制密钥管理"),
    ("安全认证","SEC-02","OAuth2.0/OIDC企业SSO集成","48","95","仅本地JWT认证，无企业IdP","集成OAuth2.0→OIDC→Keycloak/AzureAD→SAML","Authlib+Keycloak/AzureAD/Okta OIDC","15","P0","SEC-01","企业IdP对接，SSO单点登录"),
    ("安全认证","SEC-03","多因素认证(MFA/TOTP/WebAuthn)","48","95","仅密码认证","TOTP→WebAuthn→设备信任→恢复码","pyotp+WebAuthn库+MFA流程","12","P0","SEC-02","两种MFA方式"),
    ("安全认证","SEC-04","API速率限制(slowapi+Redis)","48","95","完全无速率限制","slowapi→分级规则→令牌桶→429友好提示","slowapi+Redis后端+可配置规则","5","P0","无","所有端点有速率限制"),
    ("安全认证","SEC-05","CSRF/XSS/CSP安全头防御","48","95","无CSRF/CSP安全头","CSRF中间件→CSP头→HTML转义→SQL参数化审计","Starlette CSRF+helmet安全头+bleach","8","P0","无","OWASP Top10全覆盖"),
    ("安全认证","SEC-06","HSTS/TLS/安全响应头","48","95","缺少HSTS等安全响应头","HSTS→X-Frame→X-Content-Type→HTTPS重定向→CORS白名单","SecureMiddleware统一安全头","3","P0","无","Mozilla Observatory A+, SSL Labs A+"),
    ("安全认证","SEC-07","敏感数据加密与日志脱敏","48","95","API key明文存储，日志泄露风险","字段AES-256加密→日志脱敏Filter→传输加密审计","Fernet/AES-256+logging.Filter","10","P0","SEC-01","敏感数据加密，日志自动脱敏"),
    ("安全认证","SEC-08","SAST/SCA/容器安全扫描","48","95","无自动化安全扫描","Bandit/Semgrep SAST→Safety/Snyk SCA→Trivy容器→ZAP DAST","Bandit+Safety+Trivy+OWASP ZAP CI","5","P0","无","CI零高危漏洞"),
    ("安全认证","SEC-09","JWT黑名单与会话管理","48","95","JWT无法主动失效","Refresh Token轮换→黑名单(Redis)→全局登出→设备管理","Redis JWT黑名单+rotation","10","P0","SEC-01","主动登出+设备管理"),
    ("安全认证","SEC-10","RBAC细粒度权限模型(Casbin)","48","95","权限模型简单无角色矩阵","Role/Permission模型→角色继承→资源级权限→动态装饰器","Casbin/Oso RBAC引擎","15","P0","SEC-02","角色创建、资源级权限"),
    ("安全认证","SEC-11","API Key与Service Account管理","48","95","无API Key机制","Key模型(scope/expiry)→Service Account→使用量→过期通知","API Key+scoped permissions+tracking","8","P1","SEC-10","API Key创建/吊销/监控"),
    ("安全认证","SEC-12","IP白名单与异常登录检测","48","95","无IP级访问控制","IP白名单→API Key绑定IP→异地登录→VPN检测","IP过滤中间件+行为分析","8","P1","SEC-04","异常登录实时告警"),
    ("安全认证","SEC-13","威胁建模与安全评审制度","48","95","无正式安全评审流程","STRIDE威胁建模→安全设计模板→检查清单→安全Champion","STRIDE方法论+评审制度化","10","P1","无","新功能通过安全评审"),
    ("安全认证","SEC-14","GDPR数据删除与保留策略","48","95","无用户数据删除和自动清理","软删除→GDPR流程→保留策略→自动归档/清理","Soft delete+数据保留策略+定时清理","10","P1","无","用户数据完整可删除"),

    # ═══ TESTING ═══
    ("测试保障","TEST-01","测试覆盖率门禁(pytest-cov≥80%)","65","90","无覆盖率配置和CI门禁","pytest-cov→.coveragerc→CI门禁≥80%→增量覆盖率","pytest-cov+coverage threshold+CI gate","5","P1","无","CI覆盖率≥80%方可合入"),
    ("测试保障","TEST-02","端到端测试自动化(Playwright)","65","90","无E2E自动化测试","Playwright→核心流程E2E→CI集成→可视化报告","Playwright+CI并行+HTML报告","12","P1","无","核心流程E2E通过"),
    ("测试保障","TEST-03","消费者驱动契约测试(Pact)","65","90","无前后端接口契约验证","Pact→consumer/provider契约→Pact Broker→CI验证","Pact-python+Pact Broker","10","P2","API-03","接口变更兼容性自动验证"),
    ("测试保障","TEST-04","性能/负载测试(k6)","65","90","无任何性能基线和回归","k6→关键API基准→CI性能回归→阈值告警","k6+GitHub Actions+基线","8","P1","无","性能基线建立，回归超标告警"),
    ("测试保障","TEST-05","多租户数据隔离验证测试","65","90","无租户数据隔离自动化验证","CRUD跨租户→并发隔离→权限隔离→CI执行","Pytest+Tenant隔离fixture","8","P1","SEC-10","零跨租户数据泄露"),
    ("测试保障","TEST-06","混沌工程与故障注入测试","65","90","无系统韧性验证","Chaos Toolkit→网络延迟→进程杀死→依赖不可用→恢复验证","Chaos Toolkit+fault injection","10","P2","OBS-06","故障自动恢复"),
    ("测试保障","TEST-07","测试数据工厂与Fixture标准化","65","90","测试数据硬编码","Factory Boy→conftest共享fixtures→事务回滚","Factory Boy+pytest fixtures","5","P2","无","测试数据统一管理"),
    ("测试保障","TEST-08","快照测试与API回归防护","65","90","无API响应快照对比","Pytest快照→API响应→LLM输出→视觉回归","syrupy+pytest-snapshot","5","P2","API-03","API变更被感知"),
    ("测试保障","TEST-09","安全测试自动化集成","65","90","无自动化安全测试在CI","ZAP baseline scan→CI集成→按严重度阻断","OWASP ZAP+CI pipeline","5","P1","SEC-08","中高危漏洞阻断发布"),

    # ═══ OBSERVABILITY ═══
    ("可观测性","OBS-01","结构化日志(JSON/loguru)","30","95","完全无结构化日志，标准logging纯文本","loguru→JSON→trace_id注入→ELK/Loki→日志查询","loguru+python-json-logger+trace_id","5","P0","无","所有日志JSON格式，含trace_id"),
    ("可观测性","OBS-02","分布式追踪(OpenTelemetry)","30","95","无分布式追踪","OTel SDK→自动插桩→Span传播→Jaeger可视化","OpenTelemetry Python+Jaeger/Tempo","10","P0","OBS-01","端到端trace可视化"),
    ("可观测性","OBS-03","指标监控(Prometheus+Grafana)","30","95","无任何业务和基础设施指标","prometheus_client→请求量/延迟/错误→Grafana→AlertManager","Prometheus+Grafana+AlertManager","8","P0","无","核心指标面板+告警就绪"),
    ("可观测性","OBS-04","告警与On-Call值班机制","30","95","无告警系统","AlertManager规则→分级告警→PagerDuty→告警收敛→On-Call","AlertManager+PagerDuty+收敛","8","P0","OBS-03","告警覆盖,On-Call运转"),
    ("可观测性","OBS-05","健康检查与K8s探针","30","95","无健康检查端点","/health/live→/health/ready(DB/Redis/LLM)→K8s探针","FastAPI health+K8s probe","3","P0","无","K8s探针配置生效"),
    ("可观测性","OBS-06","错误追踪(Sentry)","30","95","无异常聚合平台","Sentry SDK→自动捕获→堆栈上下文→Release追踪","Sentry SDK+release tracking","5","P0","OBS-01","异常自动聚合通知"),
    ("可观测性","OBS-07","LLM调用全链路可观测","30","95","LLM调用完全黑盒","Token计数→延迟→成本→质量评估→Dashboard","LangSmith/Lunary+自定义中间件","10","P0","OBS-02","Token/延迟/成本全链路可视"),
    ("可观测性","OBS-08","中间件(Redis/DB)连接池监控","30","95","无中间件级别监控","连接池使用率→慢查询→内存→Dashboard","Redis/PostgreSQL exporter+Grafana","5","P1","OBS-03","中间件指标面板就绪"),
    ("可观测性","OBS-09","前端真实用户监控(RUM)","30","95","无前端性能数据","Web Vitals→关键交互耗时→JS错误率→转化漏斗","Web Vitals+RUM SDK+错误收集","10","P2","OBS-03","前端性能看板可用"),
    ("可观测性","OBS-10","服务依赖拓扑可视化","30","95","服务调用关系不透明","OTel→服务拓扑→延迟热力图→依赖健康度","OpenTelemetry Service Map+Grafana","6","P0","OBS-02","服务拓扑可视化"),
    ("可观测性","OBS-11","动态日志级别调整","30","95","日志级别硬编码在代码","Admin API→动态调整→按模块→自动恢复→操作审计","loguru dynamic level+Admin API","3","P2","OBS-01","运行时调整日志级别"),
    ("可观测性","OBS-12","SLA/SLO/SLI定义与Error Budget","30","95","无SLA概念","定义SLO→SLI指标→Error Budget→Burn Rate告警","SLO Dashboard+Error Budget","8","P1","OBS-03","SLO达标率可视化"),
    ]

    for i,(dim,id,title,sc,tg,desc,act,tech,days,pri,dep,acc) in enumerate(T):
        r=2+i;gap=int(tg)-int(sc)
        vals=[dim,id,title,int(sc),int(tg),gap,desc,act,tech,int(days),pri,dep,acc]
        for j,v in enumerate(vals,1):
            c=ws.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
            if i%2==0:c.fill=DIM
        ws.cell(row=r,column=4).alignment=CA;ws.cell(row=r,column=5).alignment=CA
        ws.cell(row=r,column=6).alignment=CA;ws.cell(row=r,column=10).alignment=CA
        ws.cell(row=r,column=11).alignment=CA;ws.cell(row=r,column=12).alignment=CA
        # Score color
        sc_c=ws.cell(row=r,column=4)
        if int(sc)<50:sc_c.fill=RED;sc_c.font=Font(name="Arial",bold=True,size=10,color="FFC62828")
        elif int(sc)<70:sc_c.fill=YEL;sc_c.font=Font(name="Arial",bold=True,size=10,color="FFE65100")
        else:sc_c.fill=GRN;sc_c.font=Font(name="Arial",bold=True,size=10,color="FF2E7D32")
        # Gap
        gap_c=ws.cell(row=r,column=6);gap_c.font=Font(name="Arial",bold=True,size=9,color="FFC62828")
        # Priority
        pri_c=ws.cell(row=r,column=11)
        if "P0" in pri:pri_c.fill=P0F;pri_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        elif "P1" in pri:pri_c.fill=P1F;pri_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        else:pri_c.fill=P2F;pri_c.font=Font(name="Arial",bold=True,size=9)
        # Completion %
        ws.cell(row=r,column=14,value=0);ws.cell(row=r,column=14).alignment=CA;ws.cell(row=r,column=14).font=NF
        ws.cell(row=r,column=16,value="未开始");ws.cell(row=r,column=16).alignment=CA;ws.cell(row=r,column=16).font=NF

    wb.save(output_path)
    print(f"Part 2a done: {len(T)} tasks written (rows 2-{1+len(T)})")



# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import os as _os

    _default = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")
    _parser = argparse.ArgumentParser(description="Populate tasks (Part 2a: ARCH/API/SEC/TEST/OBS)")
    _parser.add_argument("--output", "-o", default=_default, help=f"Output xlsx path (default: {{_default}})")
    _args = _parser.parse_args()
    main(_args.output)
