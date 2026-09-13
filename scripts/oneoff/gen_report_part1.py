#!/usr/bin/env python3
"""Part 1: Generate evaluation report - Dashboard + Task list header"""
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import date

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def main(output_path: str) -> None:
    """Create workbook with first 4 sheets and save to output_path."""
    wb = Workbook()
    HDR_FILL = PatternFill("solid", fgColor="1B2A4A")
    HDR_FONT = Font(name="Arial", bold=True, color="FFFFFFFF", size=10)
    DIM_FILL = PatternFill("solid", fgColor="F0F2F5")
    SECT_FILL = PatternFill("solid", fgColor="2E5090")
    SECT_FONT = Font(name="Arial", bold=True, color="FFFFFFFF", size=11)
    NF = Font(name="Arial", size=9)
    BF = Font(name="Arial", bold=True, size=9)
    RED = PatternFill("solid", fgColor="FFCDD2")
    YEL = PatternFill("solid", fgColor="FFF9C4")
    GRN = PatternFill("solid", fgColor="C8E6C9")
    P0F = PatternFill("solid", fgColor="E53935")
    P1F = PatternFill("solid", fgColor="FB8C00")
    P2F = PatternFill("solid", fgColor="FDD835")
    TB = Border(left=Side("thin","D0D0D0"),right=Side("thin","D0D0D0"),top=Side("thin","D0D0D0"),bottom=Side("thin","D0D0D0"))
    BB = Border(left=Side("medium","1B2A4A"),right=Side("medium","1B2A4A"),top=Side("medium","1B2A4A"),bottom=Side("medium","1B2A4A"))
    CA = Alignment(horizontal="center", vertical="center", wrap_text=True)
    LA = Alignment(horizontal="left", vertical="center", wrap_text=True)

    def hdr(ws,row,vals):
        for i,v in enumerate(vals,1):
            c=ws.cell(row=row,column=i,value=v);c.font=HDR_FONT;c.fill=HDR_FILL;c.alignment=CA;c.border=BB

    def sectitle(ws,row,title,cols):
        ws.merge_cells(start_row=row,start_column=1,end_row=row,end_column=cols)
        c=ws.cell(row=row,column=1,value=title);c.font=SECT_FONT;c.fill=SECT_FILL;c.alignment=LA
        for i in range(1,cols+1):ws.cell(row=row,column=i).border=BB;ws.cell(row=row,column=i).fill=SECT_FILL

    # ═══ Sheet 1: Dashboard ═══
    ws1=wb.active;ws1.title="评估总览"
    sectitle(ws1,1,"Zhanlu 企业级智能体应用系统 — 多维评估报告",10)
    ws1.merge_cells("B1:J1")
    ws1.cell(row=2,column=1,value=f"评估日期: {date.today().isoformat()}  |  对标: 顶级企业级Agent系统  |  基准分: 100").font=Font(name="Arial",size=9,italic=True,color="FF888888")

    hdr(ws1,4,["评估维度","得分","标准","差距","成熟度","优先级","缺口","权重%","加权得分","评级"])
    dims=[
        ("1.架构设计与代码质量",72,95,"中等偏上","P1-重要",8,15),
        ("2.API设计与文档",62,95,"中等","P1-重要",10,10),
        ("3.安全与身份认证",48,95,"初级","P0-紧急",14,12),
        ("4.测试与质量保障",65,90,"中等","P1-重要",9,10),
        ("5.可观测性与监控",30,95,"起步","P0-紧急",12,10),
        ("6.数据管理与持久化",68,90,"中等偏上","P1-重要",7,8),
        ("7.LLM/AI集成能力",78,95,"良好","P2-提升",6,10),
        ("8.多租户与隔离",55,95,"初级","P0-紧急",10,8),
        ("9.性能与可扩展性",52,90,"初级","P1-重要",11,7),
        ("10.DevOps与CI/CD",38,95,"起步","P0-紧急",11,5),
        ("11.错误处理与韧性",48,90,"初级","P1-重要",9,5),
        ("12.配置管理",68,85,"中等偏上","P2-提升",5,3),
        ("13.Agent/Skill体系",76,95,"良好","P2-提升",7,10),
        ("14.前端与用户体验",58,90,"初级","P1-重要",10,3),
        ("15.合规与治理",42,95,"起步","P0-紧急",11,4),
    ]
    total_wtd=0
    for i,(name,sc,std,mat,pri,gaps,wt) in enumerate(dims):
        r=5+i;wtd=round(sc*wt/100,1);total_wtd+=wtd
        rating="A" if sc>=80 else ("B" if sc>=70 else ("C" if sc>=50 else "D"))
        vals=[name,sc,std,std-sc,mat,pri,gaps,wt,wtd,rating]
        for j,v in enumerate(vals,1):
            c=ws1.cell(row=r,column=j,value=v);c.font=NF;c.alignment=CA;c.border=TB
        sc_c=ws1.cell(row=r,column=2)
        if sc<50:sc_c.fill=RED;sc_c.font=Font(name="Arial",bold=True,size=11,color="FFC62828")
        elif sc<70:sc_c.fill=YEL;sc_c.font=Font(name="Arial",bold=True,size=11,color="FFE65100")
        else:sc_c.fill=GRN;sc_c.font=Font(name="Arial",bold=True,size=11,color="FF2E7D32")
        pri_c=ws1.cell(row=r,column=6)
        if "P0" in pri:pri_c.fill=P0F;pri_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        elif "P1" in pri:pri_c.fill=P1F;pri_c.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        else:pri_c.fill=P2F;pri_c.font=Font(name="Arial",bold=True,size=9)

    rs=5+len(dims)
    ws1.merge_cells(start_row=rs,start_column=1,end_row=rs,end_column=1)
    ws1.cell(row=rs,column=1,value="加权综合得分").font=Font(name="Arial",bold=True,size=12,color="FF1B2A4A")
    for j in range(1,11):ws1.cell(row=rs,column=j).border=BB;ws1.cell(row=rs,column=j).fill=DIM_FILL
    ws1.cell(row=rs,column=2,value=round(total_wtd,1)).font=Font(name="Arial",bold=True,size=14,color="FF1B2A4A")
    ws1.cell(row=rs,column=2).alignment=CA

    rl=rs+2
    sectitle(ws1,rl,"关键差距 TOP 10 — 当前得分最低领域",10)
    gaps_top=[
        ("可观测性",30,"无结构化日志/无Metrics/无Tracing/无告警 — 生产环境完全不可观测"),
        ("CI/CD",38,"无自动化CI/CD流水线/无代码质量门禁/无自动部署/无回滚机制"),
        ("合规与治理",42,"无审计日志完整方案/无RBAC细粒度权限/无数据合规GDPR框架"),
        ("安全认证",48,"默认JWT密钥/无MFA/无速率限制/无CSRF/安全头缺失/无安全扫描"),
        ("错误处理",48,"无全局异常处理器/无标准化错误响应/无熔断降级/无重试策略"),
        ("多租户",55,"数据隔离不完整/无租户级配额/无租户级别资源限制/无跨租户审计"),
        ("性能扩展",52,"无水平扩展/无缓存策略/无连接池优化/无数据库读写分离"),
        ("前端体验",58,"无响应式适配/无i18n/无障碍性支持/无离线PWA/加载性能未优化"),
        ("API文档",62,"OpenAPI无自定义描述/无API版本化/响应格式不统一/无SDK生成"),
        ("测试",65,"无覆盖率门禁/集成测试不足/无端到端测试自动化/无性能测试"),
    ]
    for k,(nm,sc,desc) in enumerate(gaps_top):
        r=rl+1+k
        ws1.cell(row=r,column=1,value=nm).font=BF
        ws1.cell(row=r,column=2,value=sc).font=Font(name="Arial",bold=True,size=10,color="FFC62828")
        ws1.cell(row=r,column=2).alignment=CA
        ws1.merge_cells(start_row=r,start_column=3,end_row=r,end_column=10)
        ws1.cell(row=r,column=3,value=desc).font=NF
        for j in range(1,11):
            ws1.cell(row=r,column=j).border=TB
            if k%2==0:ws1.cell(row=r,column=j).fill=DIM_FILL

    for col,wd in {'A':26,'B':10,'C':10,'D':10,'E':13,'F':12,'G':8,'H':8,'I':10,'J':8}.items():
        ws1.column_dimensions[col].width=wd
    ws1.sheet_properties.tabColor="1B2A4A"

    # ═══ Sheet 2: Task List (setup only, data loaded in part2) ═══
    ws2=wb.create_sheet("详细任务清单")
    hdr2=["维度","编号","任务标题","得分","目标","差距","现状与问题描述","改进措施","技术方案","工期(人天)","优先级","依赖","验收标准","%","负责人","状态","备注"]
    hdr(ws2,1,hdr2)
    for col,wd in {'A':16,'B':10,'C':28,'D':7,'E':7,'F':7,'G':32,'H':38,'I':26,'J':8,'K':8,'L':10,'M':24,'N':6,'O':8,'P':8,'Q':18}.items():
        ws2.column_dimensions[col].width=wd
    ws2.sheet_properties.tabColor="2E5090"

    # ═══ Sheet 3: P0 Priority Sprint Plan ═══
    ws3=wb.create_sheet("P0紧急冲刺计划")
    hdr(ws3,1,["周次","维度","任务编号","任务标题","预估人天","关键里程碑","阻塞项","状态"])
    sprint_weeks=[
        ("Week1","安全认证","SEC-01~02","JWT密钥管理+OAuth2.0/OIDC","8+15","移除默认密钥→Vault集成→Keycloak部署","无","未开始"),
        ("Week1","安全认证","SEC-04","API速率限制(slowapi+Redis)","5","slowapi集成→分级规则→429响应→测试","SEC-01","未开始"),
        ("Week1","可观测性","OBS-01","结构化日志(JSON格式)","5","loguru集成→JSON格式→trace_id→ELK收集","无","未开始"),
        ("Week2","安全认证","SEC-05~06","CSRF/XSS/CSP安全头+HTTPS","8+3","安全头中间件→HTTPS重定向→安全扫描","SEC-01","未开始"),
        ("Week2","可观测性","OBS-03","指标监控(Prometheus+Grafana)","8","prometheus_client→业务指标→Grafana→告警","OBS-01","未开始"),
        ("Week2","可观测性","OBS-05~06","健康检查+Sentry错误追踪","3+5","health端点→K8s探针→Sentry集成","无","未开始"),
        ("Week3","安全认证","SEC-07~09","敏感数据加密+JWT黑名单+会话管理","10+10","字段加密→日志脱敏→黑名单→登出","SEC-01","未开始"),
        ("Week3","可观测性","OBS-02","分布式追踪(OpenTelemetry)","10","OTel SDK→Span传播→Jaeger→采样","OBS-01","未开始"),
        ("Week3","可观测性","OBS-04","告警与On-Call机制","8","AlertManager规则→分级告警→PagerDuty","OBS-03","未开始"),
        ("Week4","安全认证","SEC-10","RBAC细粒度权限模型(Casbin)","15","Role/Permission→资源级权限→装饰器","SEC-02","未开始"),
        ("Week4","可观测性","OBS-07","LLM调用全链路可观测","10","Token计数→延迟→成本→Dashboard","OBS-02","未开始"),
        ("Week4","可观测性","OBS-10","服务依赖拓扑可视化","6","OTel→ServiceMap→延迟热力图","OBS-02","未开始"),
        ("Week5","安全认证","SEC-03","多因素认证(MFA)","12","TOTP→WebAuthn→设备信任→恢复码","SEC-02","未开始"),
        ("Week5","DevOps","DEVOPS-01~03","CI/CD流水线+GitOps+自动化部署","10+10+12","GitHub Actions→Docker→K8s→ArgoCD","无","未开始"),
        ("Week5","多租户","MULTI-01~03","租户数据隔离+配额+资源限制","12+10+8","tenant_id过滤→配额模型→资源上限","SEC-10","未开始"),
        ("Week6","错误处理","ERR-01~03","全局异常处理+熔断+重试","5+10+8","异常映射→熔断器→重试策略→降级","OBS-02","未开始"),
        ("Week6","合规治理","COMP-01~03","审计日志+隐私保护+审批流程","12+8+10","审计表→GDPR→审批工作流","SEC-10","未开始"),
    ]
    for i,(wk,dim,ids,title,days,ms,block,st) in enumerate(sprint_weeks):
        r=2+i
        for j,v in enumerate([wk,dim,ids,title,days,ms,block,st],1):
            c=ws3.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
        ws3.cell(row=r,column=5).alignment=CA
        ws3.cell(row=r,column=8).alignment=CA
        for j in range(1,9):
            if i%2==0:ws3.cell(row=r,column=j).fill=DIM_FILL
    for col,wd in {'A':8,'B':10,'C':14,'D':32,'E':10,'F':30,'G':12,'H':10}.items():
        ws3.column_dimensions[col].width=wd
    ws3.sheet_properties.tabColor="E53935"

    # ═══ Sheet 4: Architecture Assessment Detail ═══
    ws4=wb.create_sheet("架构详细评估")
    hdr(ws4,1,["子维度","当前状态","得分(100)","顶级标准","差距","风险等级","改进方向"])
    arch_detail=[
        ("分层架构","扁平MVC，services 247文件无层次","40","清晰的领域/应用/基础设施/接口四层","60","高","DDD重构，定义有界上下文"),
        ("模块化","目录结构松散，__init__无公共API","50","高内聚低耦合，模块边界清晰","50","中","按领域重组，公共API显式声明"),
        ("依赖管理","交叉import，sys.path.insert模式","45","清晰的依赖方向，DI容器","55","高","引入DI，消除循环依赖"),
        ("接口抽象","服务间直接调用具体类","35","面向接口编程，Protocol/ABC抽象","65","高","核心服务定义接口契约"),
        ("事件驱动","同步调用链长，服务耦合","30","事件总线，异步解耦","70","中","领域事件+Redis Streams"),
        ("异步编程","异步/同步混用，阻塞事件循环","55","统一async/await，线程池CPU任务","45","中","审计替换同步阻塞调用"),
        ("代码规范","无格式化/类型检查/静态分析","40","Ruff+MyPy+pre-commit+CI门禁","60","高","引入代码质量工具链"),
        ("复杂度","agent_prompts.py 81KB，部分函数过长","50","圈复杂度<15，单文件<500行","50","中","radon检测，拆分超大文件"),
        ("可测试性","无DI，测试依赖真实服务","45","核心逻辑可独立单元测试","55","中","接口抽象后mock测试"),
        ("扩展性","新功能需改动多处","50","插件化，开闭原则","50","中","事件驱动+插件注册机制"),
    ]
    for i,(sub,cur,sc,std,gap,risk,fix) in enumerate(arch_detail):
        r=2+i
        for j,v in enumerate([sub,cur,sc,std,gap,risk,fix],1):
            c=ws4.cell(row=r,column=j,value=v);c.font=NF;c.alignment=LA;c.border=TB
        ws4.cell(row=r,column=3).alignment=CA
        ws4.cell(row=r,column=5).alignment=CA
        ws4.cell(row=r,column=6).alignment=CA
        rc=ws4.cell(row=r,column=6)
        if risk=="高":rc.fill=P0F;rc.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        elif risk=="中":rc.fill=P1F;rc.font=Font(name="Arial",bold=True,size=9,color="FFFFFFFF")
        else:rc.fill=P2F;rc.font=Font(name="Arial",bold=True,size=9)
    for col,wd in {'A':14,'B':32,'C':12,'D':36,'E':10,'F':10,'G':40}.items():
        ws4.column_dimensions[col].width=wd
    ws4.sheet_properties.tabColor="1565C0"

    wb.save(output_path)
    print(f"Part 1 saved successfully to {output_path} (Sheets: Dashboard, Task List header, P0 Sprint, Architecture Detail)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import os

    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhanlu_evaluation_report.xlsx")
    parser = argparse.ArgumentParser(description="Part 1: Generate evaluation report - Dashboard + Task list header")
    parser.add_argument("--output", "-o", default=default, help=f"Output xlsx path (default: {default})")
    args = parser.parse_args()
    main(args.output)
