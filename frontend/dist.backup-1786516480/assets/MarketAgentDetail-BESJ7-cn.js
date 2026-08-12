import{a as d,j as e}from"./query-vendor-B4t1pFSw.js";import{u as S,e as h,a as L,f as N,P as w,B as D,Z as T,ay as M,J as k,az as _,ax as P,aj as q,h as B,t as v}from"./index-DJe1vVxl.js";import{h as E,c as Q}from"./react-vendor-Dhukp042.js";import{U as R}from"./users-DgD2TlTa.js";import"./chart-vendor-CxrhSepo.js";import"./radix-vendor-CKbA1A0R.js";import"./markdown-vendor-BREGfHs4.js";function z(n,r){const s=`${n.name} ${n.description} ${(n.capabilities||[]).join(" ")}`.toLowerCase(),o=new Set,t=[{tool:"industry-research-report-writer",keywords:["report","audit","analysis","分析","报告","审计","洞察","insight"]},{tool:"pptx-generator",keywords:["presentation","ppt","幻灯片","汇报","deck"]},{tool:"visual-content-generator",keywords:["chart","visual","dashboard","图表","仪表盘","可视化","趋势"]},{tool:"topic-tracker",keywords:["trend","content","summary","趋势","摘要","舆情","监控"]},{tool:"social-media-trend-search",keywords:["social","media","社交媒体","舆情"]}];for(const{tool:i,keywords:l}of t)r.some(c=>c.name===i)&&l.some(c=>s.includes(c))&&o.add(i);return o.size===0&&r.some(i=>i.name==="industry-research-report-writer")&&o.add("industry-research-report-writer"),[...o]}function I(n){const{name:r,description:s,capabilities:o=[],knowledge_bases:t=[]}=n,i=o.length?o:["核心业务分析"],l=i.map((m,x)=>`${x+1}. ${m}`).join(`
`),y=Array.isArray(t)&&t.length>0?'\n## 数据库访问（强制要求 — 必须调用 ask_data_agent）\n当用户问题涉及已绑定的数据库/知识库时，**必须**调用函数名完全等于 `ask_data_agent` 的工具（大小写敏感）。\n这是访问数据的唯一路径；不要在回复中虚构 SQL、假装查询或描述"将要做"的步骤而不真正发起工具调用。\n\n工具签名（请使用完全一致的 `name` 字段）：\n```\nask_data_agent(\n    question: str,                # 必填 — 用自然语言描述要查的问题\n    data_source_id: str = None,   # 可选 — 指定已绑定的数据源 id\n    max_iterations: int = 6,      # 可选 — 子代理工具调用轮数上限（最大 10）\n)\n```\n\n工作流：\n1. 调用 `ask_data_agent`，传 `question` 参数\n2. 读取返回的 `answer`（自然语言）、`rows`（数据）、`sql`（实际执行的 SQL）、`source_name`（数据源）、`citations`（引用的表/列）\n3. 基于返回内容组织回复，引用 `source_name` 与相关列名\n4. 如返回空或出错，直接说明 — 不要编造数据\n\n禁止：\n- 不要调用 `list_data_sources` / `describe_schema` / `execute_query` / `answer_from_database` — 这些是子代理内部工具，不在你的工具列表上\n- 不要在回复文本中手写 SQL；SQL 只出现在 `ask_data_agent` 的返回里\n':"";return{prompt_identity:`## 身份
你是「${r}」，一位企业级 AI 智能体。

## 定位
${s}

## 专业领域
你具备以下工具的资深经验：
${l}

## 使命
为用户提供专业的${r}服务，通过数据分析和智能洞察支持业务决策与运营优化。

## 服务对象
面向企业运营、管理和专业人员，以专业、可信赖的方式交付价值。`,prompt_boundary:`## 可以执行
${i.map(m=>`- ${m}：基于授权数据进行专业分析并输出结论`).join(`
`)}
- 查询和访问已授权的知识库与业务数据源
- 生成分析报告、预警通知和可执行建议
- 监控数据变化趋势并识别异常

## 不可执行
- 访问未授权的数据源或系统
- 执行未经审批的写操作或高风险变更
- 做出超出${r}职责范围的最终业务决策
- 泄露敏感数据或违反数据合规要求

## 人工介入条件
- 检测到高风险异常或合规问题时，立即通知相关人员
- 数据不足或质量不佳导致无法可靠判断时，转人工处理
- 涉及重大财务、安全或运营影响的决策，需人工确认后执行`,prompt_reasoning:`面对用户任务时，按以下推理框架执行：

1. **需求理解**：解析用户意图，明确分析目标、时间范围和数据需求；信息不足时主动提问澄清。
2. **数据获取**：从已授权的数据源中检索和聚合相关数据，确保数据来源合规。
3. **分析诊断**：运用专业方法分析数据，识别趋势、异常和关键模式，结合行业经验给出判断。
4. **方案构建**：基于分析结果，构建可执行的建议、报告或行动计划，标注优先级和风险。
5. **结果验证**：校验输出的准确性、完整性和合规性，确保结论有数据支撑。
6. **输出呈现**：以结构化、易理解的格式呈现结果，突出关键发现和行动建议。`,prompt_tools:`## 工具调用判断
- 需要查询业务数据时 → 调用已授权的数据库/知识库连接
- 需要生成文档报告时 → 使用报告生成工具（DOCX/PPTX）
- 需要处理上传文件时 → 使用文件解析工具
- 需要外部实时信息时 → 使用网络搜索（如已启用）
${y}
## 参数规范
- 数据查询：明确指定时间范围、筛选条件和所需字段
- 报告生成：指定标题、格式和内容结构
- 文件处理：确认文件格式和解析目标

## 异常处理策略
- 工具调用失败 → 重试 3 次后降级，告知用户并提供替代方案
- 数据源不可用 → 说明情况并建议检查数据源配置
- 权限不足 → 说明限制并建议联系管理员授权`,prompt_output:`## 输出格式
使用 Markdown 格式输出，结构如下：

### 概要
简要说明分析目的、数据范围和主要发现。

### 分析详情
用表格、列表等清晰呈现关键数据和趋势分析。

### 关键洞察
突出重要发现、异常预警和风险点。

### 建议行动
给出具体、可执行的下一步行动建议，标注优先级（高/中/低）。

## 语言要求
- 专业、简洁、可执行
- 使用中文回复（除非用户指定其他语言）
- 直击重点，避免冗余`}}function K(){const{id:n}=E(),r=Q(),{t:s,lang:o}=S(),[t,i]=d.useState(null),[l,c]=d.useState([]),[y,m]=d.useState(!0),[x,b]=d.useState(!1),[g,C]=d.useState(!1);d.useEffect(()=>{$()},[n]);async function $(){try{const a=await h.entities.MarketAgent.get(n);i(a);const u=await h.entities.MarketAgent.list("-updated_date",200);c(u.filter(f=>f.category===a.category&&f.id!==a.id).slice(0,4))}catch{i(null)}finally{m(!1)}}async function A(){b(!0);try{let a=[];try{const f=await h.entities.Tool.list();a=z(t,f)}catch{}const u=await h.entities.AgentApp.create({name:t.name,description:t.description,capabilities:t.capabilities||[],model:"automatic",status:"active",data_read:!0,agent_type:"sequential",topology:"standalone",skills:a,...I(t)});C(!0),v({title:s.agentConfig.cloned,description:t.name}),setTimeout(()=>r(`/my-space/agent/${u.id}`),600)}catch(a){v({title:"Error",description:a.message,variant:"destructive"}),b(!1)}}const p=L([t==null?void 0:t.name,t==null?void 0:t.description,...((t==null?void 0:t.capabilities)||[]).filter(Boolean),...l.flatMap(a=>[a.name,a.description])].filter(Boolean),o);return y?e.jsx("div",{className:"flex h-full items-center justify-center",children:e.jsx(N,{className:"h-6 w-6 animate-spin text-muted-foreground"})}):t?e.jsxs("div",{className:"h-full overflow-y-auto px-8 py-8",children:[e.jsx(w,{title:p(t.name),subtitle:s.market.categories[t.category],action:e.jsxs(D,{onClick:A,disabled:x||g,size:"sm",className:"gap-2",children:[x?e.jsx(N,{className:"h-3.5 w-3.5 animate-spin"}):g?e.jsx(T,{className:"h-3.5 w-3.5"}):e.jsx(M,{className:"h-3.5 w-3.5"}),x?s.agentConfig.cloning:g?s.agentConfig.cloned:s.agentConfig.clone,!g&&e.jsx(k,{className:"h-3.5 w-3.5"})]})}),e.jsxs("div",{className:"mb-6 flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-4 py-2.5 text-xs text-muted-foreground",children:[e.jsx(_,{className:"h-3.5 w-3.5 shrink-0 text-primary"}),e.jsx("span",{children:s.agentConfig.readOnlyHint})]}),e.jsxs("div",{className:"grid grid-cols-1 gap-6 lg:grid-cols-3",children:[e.jsxs("div",{className:"space-y-6 lg:col-span-2",children:[e.jsxs(j,{title:s.detail.basicInfo,children:[e.jsxs("div",{className:"mb-4 flex flex-wrap items-center gap-4",children:[e.jsxs("span",{className:"inline-flex items-center gap-1 text-sm text-foreground",children:[e.jsx(P,{className:"h-4 w-4 fill-primary text-primary"})," ",t.rating]}),e.jsxs("span",{className:"inline-flex items-center gap-1 text-sm text-muted-foreground",children:[e.jsx(R,{className:"h-4 w-4"})," ",t.subscribers," ",s.market.subscribers]}),e.jsxs("span",{className:"inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-muted-foreground",children:[e.jsx(_,{className:"h-3 w-3"})," ",s.agentConfig.readOnly]})]}),e.jsx("p",{className:"text-sm leading-relaxed text-foreground",children:p(t.description)})]}),e.jsx(j,{title:s.detail.capabilities,children:t.capabilities&&t.capabilities.length>0?e.jsx("div",{className:"flex flex-wrap gap-2",children:t.capabilities.map((a,u)=>e.jsxs("span",{className:"inline-flex items-center gap-1 rounded-md bg-secondary px-2.5 py-1 text-xs text-foreground",children:[e.jsx(q,{className:"h-3 w-3 text-muted-foreground"})," ",p(a)]},u))}):e.jsx("p",{className:"text-sm text-muted-foreground",children:s.detail.noCapabilities})})]}),e.jsxs("div",{className:"space-y-6",children:[e.jsx(j,{title:s.detail.relatedAgents,children:l.length>0?e.jsx("div",{className:"space-y-2",children:l.map(a=>e.jsxs("button",{onClick:()=>r(`/market/${a.id}`),className:"flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2.5 text-left transition-colors hover:bg-secondary",children:[e.jsx("div",{className:"flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-secondary text-xs font-medium text-muted-foreground",children:a.name.charAt(0)}),e.jsxs("div",{className:"min-w-0 flex-1",children:[e.jsx("div",{className:"truncate text-sm text-foreground",children:p(a.name)}),e.jsx("div",{className:"truncate text-xs text-muted-foreground",children:p(a.description)})]}),e.jsx(k,{className:"h-3.5 w-3.5 shrink-0 text-muted-foreground"})]},a.id))}):e.jsx("p",{className:"text-sm text-muted-foreground",children:s.detail.noCapabilities})}),e.jsx(j,{title:s.detail.metadata,children:e.jsxs("div",{className:"space-y-3 text-sm",children:[e.jsx(O,{label:s.detail.createdDate,value:B(t.created_date)}),e.jsxs("div",{className:"flex items-center justify-between border-t border-border pt-3",children:[e.jsx("span",{className:"text-muted-foreground",children:"ID"}),e.jsx("span",{className:"font-mono text-xs text-muted-foreground",children:t.id.slice(-8)})]})]})})]})]})]}):e.jsx("div",{className:"px-8 py-8",children:e.jsx(w,{title:s.detail.notFound})})}function j({title:n,children:r}){return e.jsxs("div",{className:"rounded-xl border border-border bg-card p-5",children:[e.jsx("h3",{className:"mb-4 font-display text-base text-foreground",children:n}),r]})}function O({label:n,value:r}){return e.jsxs("div",{className:"flex items-center justify-between",children:[e.jsx("span",{className:"text-muted-foreground",children:n}),e.jsx("span",{className:"text-foreground",children:r})]})}export{K as default};
