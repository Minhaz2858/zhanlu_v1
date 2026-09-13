#!/usr/bin/env python3
"""Part 3: Add remaining detail sheets"""
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def main(output_path: str) -> None:
    """Add detail sheets (assessments, risks, KPIs). Saves to output_path."""
    wb=load_workbook(output_path)
    NF=Font(name="Arial",size=9);BF=Font(name="Arial",bold=True,size=9)
    HDR_FILL=PatternFill("solid",fgColor="1B2A4A")
    HDR_FONT=Font(name="Arial",bold=True,color="FFFFFFFF",size=10)
    DIM=PatternFill("solid",fgColor="F0F2F5")
    RED=PatternFill("solid",fgColor="FFCDD2");YEL=PatternFill("solid",fgColor="FFF9C4")
    GRN=PatternFill("solid",fgColor="C8E6C9")
    P0F=PatternFill("solid",fgColor="E53935");P1F=PatternFill("solid",fgColor="FB8C00")
    P2F=PatternFill("solid",fgColor="FDD835")
    TB=Border(left=Side("thin","FFD0D0D0"),right=Side("thin","FFD0D0D0"),top=Side("thin","FFD0D0D0"),bottom=Side("thin","FFD0D0D0"))
    BB=Border(left=Side("medium","FF1B2A4A"),right=Side("medium","FF1B2A4A"),top=Side("medium","FF1B2A4A"),bottom=Side("medium","FF1B2A4A"))
    CA=Alignment(horizontal="center",vertical="center",wrap_text=True)
    LA=Alignment(horizontal="left",vertical="center",wrap_text=True)

    def hdr(ws,row,vals):
        for i,v in enumerate(vals,1):
            c=ws.cell(row=row,column=i,value=v);c.font=HDR_FONT;c.fill=HDR_FILL;c.alignment=CA;c.border=BB

    # ═══ Sheet 5: Dimension-wise Detailed Assessment ═══
    ws5=wb.create_sheet("各维度详细评估")
    hdr(ws5,1,["维度","子项","当前状态","得分(100)","目标","差距","严重程度","改进方案摘要","预估工期(周)"])
    assessments=[
        # Architecture
        ("架构设计","分层架构","扁平MVC，services 247文件无层次","40","95","55","高","引入DDD四层架构，定义领域/应用/基础设施/接口层","4-6"),
        ("架构设计","模块化设计","目录松散，__init__无公共API","50","95","45","中","按有界上下文重组模块，显式声明公共API","2-3"),
        ("架构设计","依赖管理","交叉import，sys.path.insert","45","95","50","高","引入依赖注入容器，消除循环依赖","2-3"),
        ("架构设计","接口抽象","服务间直接调用具体类","35","95","60","高","定义Protocol/ABC接口，面向接口编程","3-4"),
        ("架构设计","事件驱动","同步调用链长，服务耦合","30","90","60","中","引入Redis Streams事件总线，异步解耦","3-4"),
        ("架构设计","代码规范","无格式化/类型检查/静态分析","40","90","50","高","Ruff+MyPy+pre-commit+CI门禁","1-2"),
        ("架构设计","复杂度控制","agent_prompts.py 81KB","50","90","40","中","radon/wily检测，拆分超大文件和函数","2"),
        # API
        ("API设计","OpenAPI文档","默认OpenAPI无自定义描述","35","95","60","高","response_model+Field注解+tags+交互示例","1-2"),
        ("API设计","版本化","无版本号","30","95","65","高","/api/v1/+Header版本+兼容策略","1-2"),
        ("API设计","响应标准化","分页不一致，格式各异","50","95","45","中","BaseResponse+T+统一分页+统一错误","1"),
        ("API设计","请求验证","entity接受dict无schema","40","95","55","高","Pydantic v2 model+严格验证","1-2"),
        ("API设计","SDK生成","无TS SDK","20","90","70","低","OpenAPI Generator自动生成npm包","1-2"),
        # Security
        ("安全认证","JWT密钥管理","默认dev-secret","15","95","80","严重","Vault+KMS+密钥轮换+Refresh Token","1-2"),
        ("安全认证","企业SSO","仅本地JWT","20","95","75","严重","OAuth2.0/OIDC+Keycloak/AzureAD+SAML","3-4"),
        ("安全认证","MFA","仅密码","15","95","80","严重","TOTP+WebAuthn+设备信任","2-3"),
        ("安全认证","速率限制","完全没有","0","95","95","严重","slowapi+Redis+分级规则","1"),
        ("安全认证","Web安全","无CSRF/CSP/安全头","20","95","75","严重","CSRF中间件+CSP+HSTS安全头","1"),
        ("安全认证","RBAC","权限模型简单","25","95","70","严重","Casbin/Oso+角色继承+资源级权限","2-3"),
        ("安全认证","安全扫描","无自动化扫描","15","95","80","严重","Bandit+Safety+Trivy+ZAP CI集成","1"),
        # Testing
        ("测试保障","覆盖率","无覆盖率要求和门禁","40","90","50","中","pytest-cov>=80%+CI门禁","1"),
        ("测试保障","E2E测试","无自动化E2E","30","90","60","中","Playwright+关键流程+CI+报告","2-3"),
        ("测试保障","契约测试","无接口契约","20","85","65","低","Pact+Provider/Consumer+Broker","2"),
        ("测试保障","性能测试","无性能基线","15","90","75","中","k6+关键API+CI回归","1-2"),
        ("测试保障","多租户隔离测试","无隔离验证","25","90","65","中","Pytest+Tenant隔离fixture","1"),
        # Observability
        ("可观测性","结构化日志","标准logging纯文本","15","95","80","严重","loguru+JSON+trace_id+ELK","1"),
        ("可观测性","分布式追踪","完全没有","0","95","95","严重","OpenTelemetry+Jaeger+Span传播","2"),
        ("可观测性","指标监控","无任何指标","0","95","95","严重","Prometheus+Grafana+AlertManager","1-2"),
        ("可观测性","告警系统","完全没有","0","95","95","严重","AlertManager+分级告警+PagerDuty","1-2"),
        ("可观测性","LLM可观测","LLM调用黑盒","10","95","85","严重","LangSmith/Lunary+Token+成本追踪","2"),
        # Data
        ("数据管理","读写分离","无读写分离","30","90","60","中","SQLAlchemy读写分离+pgbouncer","1-2"),
        ("数据管理","迁移自动化","Alembic手动执行","40","90","50","中","CI自动迁移+回滚+测试","1-2"),
        ("数据管理","缓存策略","无任何缓存","20","90","70","中","Redis Cache-Aside+多级缓存","2-3"),
        ("数据管理","向量数据库","嵌入存SQLite","50","90","40","低","Milvus/Qdrant+HNSW索引","3-4"),
        ("数据管理","备份恢复","无自动化方案","25","90","65","中","WAL归档+S3+定期演练","2"),
        # LLM
        ("LLM集成","多模型路由","仅OpenAI兼容","60","95","35","低","Provider抽象+模型路由+Fallback","2"),
        ("LLM集成","Prompt管理","硬编码81KB","40","95","55","低","外部化+版本管理+A/B实验","2-3"),
        ("LLM集成","质量评估","无自动化评估","30","95","65","低","LLM-as-Judge+RAGAS+标注集","2-3"),
        ("LLM集成","Token控制","计数不精确","50","95","45","低","tiktoken+预算+成本面板","1-2"),
        # Multi-tenancy
        ("多租户","数据隔离","SQLite为主不完整","40","95","55","严重","PostgreSQL RLS+tenant-aware ORM","2-3"),
        ("多租户","资源配额","无任何配额","15","95","80","严重","CPU/内存/存储/API配额+超额策略","2"),
        ("多租户","资源隔离","无租户级隔离","25","95","70","严重","并发限制+优先级队列+QoS","1-2"),
        # Performance
        ("性能扩展","水平扩展","无状态设计不完整","40","90","50","中","Redis Session+HPA+多实例","2"),
        ("性能扩展","缓存层","完全无缓存","10","90","80","中","多级缓存+CDN+查询缓存","2-3"),
        ("性能扩展","查询优化","部分N+1查询","50","90","40","中","慢查询+索引+Eager Loading","1-2"),
        # DevOps
        ("DevOps","CI/CD","完全没有","0","95","95","严重","GitHub Actions+Docker+K8s","2-3"),
        ("DevOps","GitOps","手动部署","0","90","90","严重","ArgoCD/Flux+Git单一事实源","1-2"),
        ("DevOps","IaC","无IaC","0","90","90","严重","Terraform+Remote State","2-3"),
        ("DevOps","容器编排","基础Docker","30","95","65","高","K8s+Helm+Probes+HPA","2-3"),
        # Error Handling
        ("错误处理","全局异常处理","无自定义处理器","25","90","65","中","@exception_handler+标准响应","1"),
        ("错误处理","熔断降级","无熔断保护","15","90","75","中","pybreaker+半开+自动恢复","1-2"),
        ("错误处理","重试策略","各有实现不统一","35","90","55","中","tenacity+统一装饰器","1"),
        # Config
        ("配置管理","配置中心","环境变量+pydantic","55","85","30","低","Consul/Etcd+动态刷新","2"),
        ("配置管理","Feature Flags","无功能开关","40","85","45","低","LaunchDarkly/自建+灰度","2"),
        # Agent
        ("Agent体系","工作流编排","FSM状态机，编排有限","65","95","30","低","LangGraph/DAG工作流+可视化","3-4"),
        ("Agent体系","多Agent协作","swarm模块基础","55","95","40","低","Agent间通信+任务分配+结果聚合","3-4"),
        ("Agent体系","Skill市场","技能系统有但缺市场","60","95","35","低","Skill marketplace+搜索+评分","2-3"),
        # Frontend
        ("前端体验","响应式/移动端","桌面端为主","40","90","50","高","Mobile-first+Responsive+触摸","2-3"),
        ("前端体验","国际化","无i18n","15","90","75","高","i18next+Crowdin+多语言","2-3"),
        ("前端体验","无障碍性","无a11y","10","90","80","高","WCAG 2.1 AA+ARIA+键盘+屏幕阅读器","2-3"),
        ("前端体验","PWA/离线","无离线支持","15","85","70","低","Service Worker+离线缓存+推送","2"),
        # Compliance
        ("合规治理","审计日志","基础审计不完整","30","95","65","严重","完整CRUD审计+防篡改+导出","2-3"),
        ("合规治理","GDPR","无合规框架","20","95","75","严重","数据地图+DSAR+同意管理+DPIA","3-4"),
        ("合规治理","审批工作流","基础governance模块","35","95","60","严重","多级审批+并行/串行+超时","2-3"),
        ("合规治理","SOC2/ISO","无认证准备","10","95","85","低","差距分析+政策+控制实施","8-12"),
    ]
    for i,(dim,sub,cur,sc,tg,gap,sev,plan,wks) in enumerate(assessments):
        r=2+i
        for j,v in enumerate([dim,sub,cur,sc,tg,gap,sev,plan,wks],1):
            c=ws5.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
            if i%2==0:c.fill=DIM
        for j in [4,5,6,9]:ws5.cell(row=r,column=j).alignment=CA
        ws5.cell(row=r,column=7).alignment=CA
        sc_c=ws5.cell(row=r,column=4)
        if int(sc)<50:sc_c.fill=RED;sc_c.font=Font(name="Arial",bold=True,size=9,color="FFC62828")
        elif int(sc)<70:sc_c.fill=YEL;sc_c.font=Font(name="Arial",bold=True,size=9,color="FFE65100")
        else:sc_c.fill=GRN;sc_c.font=Font(name="Arial",bold=True,size=9,color="FF2E7D32")
        sev_c=ws5.cell(row=r,column=7)
        if "严重" in sev:sev_c.fill=P0F;sev_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        elif "高" in sev:sev_c.fill=P1F;sev_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        else:sev_c.fill=P2F;sev_c.font=Font(name="Arial",bold=True,size=9)
    for col,wd in {'A':14,'B':18,'C':30,'D':10,'E':10,'F':10,'G':10,'H':38,'I':12}.items():
        ws5.column_dimensions[col].width=wd
    ws5.sheet_properties.tabColor="7B1FA2"

    # ═══ Sheet 6: Risk Matrix ═══
    ws6=wb.create_sheet("风险矩阵")
    hdr(ws6,1,["风险ID","风险描述","影响维度","概率","影响程度","风险等级","缓解措施","触发条件","责任人"])
    risks=[
        ("RISK-01","生产环境完全不可观测，故障无法发现","可观测性","高","严重","P0-紧急","立即部署Prometheus+Grafana+Sentry","任何人",""),
        ("RISK-02","默认JWT密钥导致认证被绕过","安全认证","高","严重","P0-紧急","移除默认值+强制密钥管理","生产部署时",""),
        ("RISK-03","无速率限制导致暴力攻击/资源耗尽","安全认证","高","严重","P0-紧急","集成slowapi+Redis限流","上线后",""),
        ("RISK-04","无CI/CD流水线导致人工部署错误","DevOps","高","高","P0-紧急","GitHub Actions+Docker+K8s ArgoCD","每次部署",""),
        ("RISK-05","多租户数据隔离不完整导致数据泄露","多租户","中","严重","P0-紧急","PostgreSQL RLS+tenant-aware ORM","高并发/故意攻击",""),
        ("RISK-06","LLM Token成本不可控导致预算超支","LLM集成","高","中","P1-重要","精确tiktoken计数+预算+告警","大量使用后",""),
        ("RISK-07","无数据库备份导致数据丢失无法恢复","数据管理","低","严重","P1-重要","自动化备份+WAL归档+定期演练","硬件故障",""),
        ("RISK-08","无熔断降级导致级联故障","错误处理","中","高","P1-重要","pybreaker+降级策略+功能开关","下游故障",""),
        ("RISK-09","无安全扫描导致已知漏洞上线","安全认证","中","高","P1-重要","Bandit+Safety+Trivy+CI集成","引入新依赖",""),
        ("RISK-10","配置硬编码导致生产事故","配置管理","中","中","P2-提升","配置中心+验证+审计+灰度","配置变更",""),
        ("RISK-11","前端无i18n限制国际化市场","前端体验","中","中","P2-提升","i18next+Crowdin+多语言","拓展国际市场",""),
        ("RISK-12","无GDPR合规框架面临法律风险","合规治理","低","严重","P1-重要","数据地图+DSAR+DPIA+数据删除","上线欧盟",""),
        ("RISK-13","无性能测试导致上线后系统崩溃","测试保障","中","高","P1-重要","k6+关键API+CI回归+弹性伸缩","高并发",""),
        ("RISK-14","依赖漏洞导致供应链攻击","合规治理","中","高","P1-重要","SBOM+依赖扫描+自动更新","引入恶意包",""),
        ("RISK-15","Agent输出无安全护栏产生风险内容","合规治理","中","高","P1-重要","AI Safety+偏见检测+Guardrails","面向外部用户",""),
    ]
    for i,(id,desc,dim,prob,imp,level,mit,trig,owner) in enumerate(risks):
        r=2+i
        for j,v in enumerate([id,desc,dim,prob,imp,level,mit,trig,owner],1):
            c=ws6.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
            if i%2==0:c.fill=DIM
        for j in [1,4,5,6,9]:ws6.cell(row=r,column=j).alignment=CA
        level_c=ws6.cell(row=r,column=6)
        if   "P0" in level:level_c.fill=P0F;level_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        elif "P1" in level:level_c.fill=P1F;level_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        else:level_c.fill=P2F;level_c.font=Font(name="Arial",bold=True,size=9)
    for col,wd in {'A':10,'B':34,'C':12,'D':8,'E':10,'F':12,'G':38,'H':14,'I':8}.items():
        ws6.column_dimensions[col].width=wd
    ws6.sheet_properties.tabColor="C62828"

    # ═══ Sheet 7: Completion Summary & KPIs ═══
    ws7=wb.create_sheet("交付评审与KPI")
    hdr(ws7,1,["指标类别","KPI指标","当前状态","目标值","衡量方式","达成路径","权重"])
    kpis=[
        ("质量","代码覆盖率","未知","≥80%","pytest-cov CI报告","覆盖率工具+CI门禁","8%"),
        ("质量","API文档完整度","<30%","100%","OpenAPI覆盖率","Field注解+example全覆盖","5%"),
        ("安全","漏洞数量","未知","零高危","SAST/SCA/Docker扫描","自动化安全扫描CI","10%"),
        ("安全","认证方式","JWT密码","OIDC+MFA","认证审计","Keycloak+认证强化","8%"),
        ("可观测性","MTTD(平均发现时间)","未知(小时级)","<5min","告警响应时间","Prometheus+AlertManager","10%"),
        ("可观测性","MTTR(平均恢复时间)","未知(小时级)","<30min","故障恢复时间","Runbook+自动化恢复","8%"),
        ("可靠性","系统可用性(SLA)","未度量","99.9%","Prometheus Uptime","健康检查+自恢复","10%"),
        ("性能","API P95延迟","未度量","<500ms","k6性能测试","缓存+索引+异步","6%"),
        ("DevOps","部署频率","手动不规律","每日多次","部署次数统计","CI/CD自动化","5%"),
        ("DevOps","变更失败率","未度量","<5%","部署成功率","GitOps+灰度+回滚","6%"),
        ("DevOps","Lead Time","未度量","<1天","代码提交到上线","CI/CD流水线","5%"),
        ("用户体验","i18n覆盖率","0%","≥3语言","语言文件覆盖检查","i18next+Crowdin","4%"),
        ("用户体验","Lighthouse评分","未度量","≥90","Lighthouse CI","性能优化+CDN","5%"),
        ("合规","GDPR合规度","<10%","100%","合规检查清单","DSAR+数据地图+同意","5%"),
        ("合规","SOC2准备度","<10%","100%","SOC2 checklist","政策+控制+审计","5%"),
    ]
    for i,(cat,kpi,cur,tgt,measure,path,wt) in enumerate(kpis):
        r=2+i
        for j,v in enumerate([cat,kpi,cur,tgt,measure,path,wt],1):
            c=ws7.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
            if i%2==0:c.fill=DIM
    for col,wd in {'A':10,'B':20,'C':14,'D':14,'E':22,'F':28,'G':6}.items():
        ws7.column_dimensions[col].width=wd
    ws7.sheet_properties.tabColor="2E7D32"

    wb.save(output_path)
    print(f"Part 3 done. Sheets added: 各维度详细评估 ({len(assessments)} items), 风险矩阵 ({len(risks)} items), 交付评审与KPI ({len(kpis)} items)")
    print(f"Final file: {output_path}")



# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import os as _os

    _default = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")
    _parser = argparse.ArgumentParser(description="Add detail sheets (assessments, risks, KPIs)")
    _parser.add_argument("--output", "-o", default=_default, help=f"Output xlsx path (default: {{_default}})")
    _args = _parser.parse_args()
    main(_args.output)
