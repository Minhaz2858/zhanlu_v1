import{a as l,j as e}from"./query-vendor-B4t1pFSw.js";import{S as w,F as v,i as F,t as Z,g as D}from"./anonymousId-DT89l1Qj.js";import{P as H}from"./ProjectSelector-D6DAOUVx.js";import{L as $}from"./LlmModelSelector-CMGZ17F0.js";import{c as z,f as j,u as O,s as B,aF as P,bb as _,al as A,af as U,g as X,a1 as M}from"./index-D3xWkbln.js";import{S as J}from"./scroll-text-abTlw6lt.js";/**
 * @license lucide-react v0.475.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const V=[["path",{d:"M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z",key:"1a0edw"}],["path",{d:"M12 22V12",key:"d0xqtd"}],["polyline",{points:"3.29 7 12 12 20.71 7",key:"ousv84"}],["path",{d:"m7.5 4.27 9 5.15",key:"1c824w"}]],W=z("Package",V);/**
 * @license lucide-react v0.475.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const Y=[["circle",{cx:"18",cy:"15",r:"3",key:"gjjjvw"}],["circle",{cx:"9",cy:"7",r:"4",key:"nufk8"}],["path",{d:"M10 15H6a4 4 0 0 0-4 4v2",key:"1nfge6"}],["path",{d:"m21.7 16.4-.9-.3",key:"12j9ji"}],["path",{d:"m15.2 13.9-.9-.3",key:"1fdjdi"}],["path",{d:"m16.6 18.7.3-.9",key:"heedtr"}],["path",{d:"m19.1 12.2.3-.9",key:"1af3ki"}],["path",{d:"m19.6 18.7-.4-1",key:"1x9vze"}],["path",{d:"m16.8 12.3-.4-1",key:"vqeiwj"}],["path",{d:"m14.3 16.6 1-.4",key:"1qlj63"}],["path",{d:"m20.7 13.8 1-.4",key:"1v5t8k"}]],G=z("UserCog",Y);function Q(){const[t,o]=l.useState([]);l.useEffect(()=>{let s=!1;return j.entities.Project.list("-updated_date",500).then(r=>{s||o(r||[])}).catch(()=>{s||o([])}),()=>{s=!0}},[]);const a=l.useMemo(()=>{const s=new Map;for(const r of t)s.set(r.name,r);return s},[t]),u=l.useMemo(()=>{const s=new Map;for(const r of t)s.set(r.id,r);return s},[t]),p=l.useCallback(s=>{if(!s||s===""||s==="all")return{project_id:null,project:"global"};if(s==="global")return{project_id:null,project:"global"};const r=u.get(s);if(r)return{project_id:r.id,project:r.name};const i=a.get(s);return i?{project_id:i.id,project:i.name}:{project_id:null,project:s}},[u,a]),h=l.useCallback((s,r)=>{const{project_id:i,project:m}=p(s);r({project_id:i,project:m})},[p]),c=l.useCallback(async(s,r,i={})=>{if(!s)return;const{project_id:m}=p(r);try{if(m){const g=await j.entities.ProjectAgent.filter({project_id:m,agent_id:s}).catch(()=>[]);if(!Array.isArray(g)||g.length===0)try{await j.entities.ProjectAgent.create({project_id:m,agent_id:s,role:"primary"})}catch(k){console.warn("ProjectAgent membership create skipped:",k)}}}catch(g){console.warn("syncProjectMembership failed (non-fatal):",g)}},[p]);return{resolve:p,resolveProjectChange:h,projects:t,syncProjectMembership:c}}function ce({form:t,update:o,t:a,isRoot:u}){const{lang:p}=O(),{resolveProjectChange:h,syncProjectMembership:c}=Q();return e.jsx(w,{title:a.agentConfig.role,desc:a.agentConfig.roleDesc,icon:G,children:e.jsxs("div",{className:"space-y-4",children:[e.jsx(v,{label:a.agentConfig.name,hint:a.agentConfig.nameHint,children:e.jsx("input",{value:t.name,onChange:s=>o({name:s.target.value}),className:F,placeholder:a.agentConfig.namePh})}),e.jsx(v,{label:a.agentConfig.description,hint:a.agentConfig.descHint,children:e.jsx("textarea",{value:t.description,onChange:s=>o({description:s.target.value}),rows:2,className:`${Z} resize-none`,placeholder:a.agentConfig.descPh})}),u&&e.jsx(v,{label:a.agentConfig.project,hint:a.agentConfig.projectHint,children:e.jsx(H,{value:t.project_id||t.project||"global",onChange:s=>{h(s,o),t.id&&c(t.id,s)}})}),e.jsx($,{value:t.llm_model_id||null,onChange:s=>o({llm_model_id:s}),disabled:!u})]})})}const le={value:"enterprise"},S=[{key:"prompt_identity",zh:"角色定位",en:"Identity",descZh:"定义智能体的身份、人格、专业领域与核心使命，是路由调度的依据",descEn:"Define identity, persona, domain and mission; basis for routing",guideZh:["身份与人格","专业领域与资深程度","核心使命与价值","语气与交互风格","服务对象与场景"],guideEn:["Identity & persona","Domain & seniority","Core mission & value","Tone & style","Audience & scenarios"],phZh:`## 身份
你是【XX 领域】的资深专家，具备 N 年实战经验，服务于【生产/质量/运维…】场景。

## 使命
负责…，目标是…。

## 语气
专业、简洁、可执行，优先给出下一步行动。`,phEn:`## Identity
You are a senior expert in [XX domain], serving [scenario].

## Mission
Responsible for…, aiming to…

## Tone
Professional, concise, actionable.`},{key:"prompt_boundary",zh:"边界约束",en:"Constraint",descZh:"红线与能力边界，明确不可执行项、合规要求、隐私约束与人工介入触发条件",descEn:"Red lines, boundaries, compliance, privacy and human escalation triggers",guideZh:["红线清单","能力边界（可/不可）","范围外拒绝或转交","人工介入触发条件","数据与隐私约束","监管合规要求"],guideEn:["Hard red lines","Capability boundary (can/cannot)","Out-of-scope handling","Human escalation triggers","Data & privacy constraints","Regulatory compliance"],phZh:`## 红线（绝对不可）
- 不得越权修改生产数据
- 不得输出涉密/PII 信息

## 能力边界
可以：…
不可：…

## 人工介入触发
- 置信度 < 80% 时转人工
- 连续失败 3 次时转人工`,phEn:`## Red lines (never)
- No unauthorized data changes
- No PII/classified output

## Boundary
Can:…  Cannot:…

## Escalate when
- confidence < 80%
- 3 consecutive failures`},{key:"prompt_reasoning",zh:"推理流程",en:"Process",descZh:"强制思维链路：分析 → 拆解 → 执行 → 验证 → 输出，含决策分支与自检",descEn:"Forced chain: analyze → decompose → execute → verify → output, with branching & self-check",guideZh:["强制思维链（CoT）","决策分支与走向","澄清策略（信息不足时提问）","异常处理与降级推理","输出前自检校验"],guideEn:["Mandatory CoT","Decision branching","Clarification strategy","Exception & fallback reasoning","Pre-output self-check"],phZh:`面对任务时按以下步骤推理：
1. 分析：识别意图与关键参数，判断是否在能力范围内
2. 拆解：将任务分解为可执行子步骤
3. 执行：逐步调用工具/技能，记录中间结果
4. 验证：校验结果完整性、准确性与一致性
5. 输出：按输出规范组装最终回复

信息不足时主动提问澄清，而非臆测。`,phEn:`Follow these steps:
1. Analyze: identify intent & key params, check scope
2. Decompose: break into sub-steps
3. Execute: invoke tools, record intermediates
4. Verify: check completeness, accuracy, consistency
5. Output: assemble per output spec

Ask clarifying questions when info is insufficient.`},{key:"prompt_tools",zh:"职能与工具",en:"Capability & Tools",descZh:"核心职能定义 + 技能(tools)调用策略：用途、判断条件、参数规范、异常降级与编排",descEn:"Tools + tool invocation: purpose, conditions, params, fallback & orchestration",guideZh:["核心职能清单","工具清单与用途说明","调用条件（何时用哪个）","参数规范与入参校验","异常降级与重试策略","多工具编排逻辑"],guideEn:["Core duties","Tool catalog & purpose","Invocation conditions","Parameter specs","Fallback & retry strategy","Multi-tool orchestration"],phZh:`## 核心职能
1. … 2. …

## 工具与调用策略
- 技能 \`production-efficiency-analysis\`：当需要分析产线效率时调用，入参：产线ID、时间范围
- 技能 \`oee-trend-analysis\`：当需要设备 OEE 趋势时调用

## 异常降级
工具失败时重试 2 次，仍失败则降级为人工提示。

## 编排
先调用 A 获取数据，再调用 B 生成图表。`,phEn:`## Core duties
1. … 2. …

## Tools & strategy
- Skill \`xxx\`: invoke when…, params: …

## Fallback
Retry 2×, then degrade to human prompt.

## Orchestration
Call A for data, then B for chart.`},{key:"prompt_output",zh:"输出规范",en:"Output",descZh:"输出格式、结构、字段强制约束，含 Few-shot 示例、引用溯源与边界输出",descEn:"Mandatory format, fields, few-shot examples, citation & edge-case outputs",guideZh:["输出格式（JSON/Markdown/表格）","必填字段与结构","语言与语气","Few-shot 示例样本","引用与防幻觉要求","边界输出（无结果/异常）"],guideEn:["Output format (JSON/MD/table)","Required fields & structure","Language & tone","Few-shot examples","Citation & anti-hallucination","Edge-case outputs"],phZh:`## 格式
输出必须为 Markdown，关键结论用 JSON 代码块。

## 必填字段
\`\`\`json
{"summary": "...", "metrics": [...], "recommendation": "..."}
\`\`\`

## 示例
用户问：…
输出：…

## 防幻觉
所有数据须标注来源；无数据时回复"暂无可用数据"。`,phEn:`## Format
Output as Markdown; key results in JSON block.

## Fields
\`\`\`json
{"summary": "...", "metrics": [...], "recommendation": "..."}
\`\`\`

## Example
Q: …  A: …

## Anti-hallucination
Cite sources; reply "No data available" when empty.`}];function T(t,o){return{key:t.key,label:o==="en"?t.en:t.zh,desc:o==="en"?t.descEn:t.descZh,guide:o==="en"?t.guideEn:t.guideZh,placeholder:o==="en"?t.phEn:t.phZh}}const C=[{value:"standalone",zh:"独立运行",en:"Standalone",descZh:"单一智能体独立完成任务",descEn:"Single agent completes alone"},{value:"sequence",zh:"顺序协作",en:"Sequence",descZh:"多智能体按序接力处理",descEn:"Agents run in sequence"},{value:"loop",zh:"循环协作",en:"Loop",descZh:"多智能体循环迭代优化",descEn:"Agents iterate in a loop"},{value:"parallel",zh:"并行协作",en:"Parallel",descZh:"多智能体并行处理后聚合",descEn:"Agents work in parallel then aggregate"}];C.filter(t=>t.value!=="standalone");const de={sequence:"SequentialAgent",loop:"LoopAgent",parallel:"ParallelAgent"},K=C.map(t=>t.value);function ue(t){return K.includes(t)?t:t==="sequential"||t?"sequence":"standalone"}function pe(t,o){const a=C.find(u=>u.value===t)||C[0];return{...a,label:o==="en"?a.en:a.zh,desc:o==="en"?a.descEn:a.descZh}}function me({form:t,update:o,t:a}){var s;const{lang:u}=O(),[p,h]=l.useState(0),c=T(S[p],u);return e.jsxs(w,{title:a.agentConfig.promptEng,desc:a.agentConfig.promptDesc,icon:J,children:[e.jsx("div",{className:"mb-4 flex flex-wrap gap-1 border-b border-border pb-2",children:S.map((r,i)=>{const m=T(r,u);return e.jsxs("button",{onClick:()=>h(i),className:`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${p===i?"bg-secondary text-foreground":"text-muted-foreground hover:text-foreground"}`,children:[e.jsxs("span",{className:"mr-1 font-mono text-muted-foreground/60",children:["L",i+1]}),m.label]},r.key)})}),e.jsx("p",{className:"mb-2 text-xs text-muted-foreground",children:c.desc}),((s=c.guide)==null?void 0:s.length)>0&&e.jsx("div",{className:"mb-3 flex flex-wrap gap-1.5",children:c.guide.map((r,i)=>e.jsxs("span",{className:"inline-flex items-center gap-1 rounded-md bg-secondary/60 px-2 py-0.5 text-[11px] text-muted-foreground",children:[e.jsx("span",{className:"h-1 w-1 rounded-full bg-primary/60"})," ",r]},i))}),e.jsx("textarea",{value:t[c.key]||"",onChange:r=>o({[c.key]:r.target.value}),rows:10,className:`${Z} resize-y font-mono text-xs leading-relaxed`,placeholder:c.placeholder}),e.jsxs("div",{className:"mt-3 flex items-center justify-between text-[11px] text-muted-foreground",children:[e.jsxs("span",{children:[a.agentConfig.layer," ",p+1," / ",S.length]}),e.jsxs("span",{children:[(t[c.key]||"").length," chars"]})]})]})}function ee(t){return t.split(/[,，;；、/]/g).map(o=>o.trim()).filter(Boolean)}function ne(t){return Array.isArray(t)?t.map(o=>String(o).trim()).filter(Boolean):typeof t!="string"?[]:ee(t)}function he({form:t,update:o,t:a}){const[u,p]=l.useState([]),[h,c]=l.useState([]),[s,r]=l.useState(!1),[i,m]=l.useState(""),[g,k]=l.useState(!1),[b,L]=l.useState("all"),x=l.useRef(null),f=ne(t==null?void 0:t.skills);l.useEffect(()=>{Promise.all([j.entities.Tool.list(),j.auth.me().catch(()=>null)]).then(([n,d])=>{const y=(d==null?void 0:d.id)||D();p(y?n.filter(N=>N.created_by_id===y):[])}).catch(()=>{})},[]),l.useEffect(()=>{if(s){if(x.current&&clearTimeout(x.current),!i.trim()){c([]);return}return k(!0),x.current=setTimeout(async()=>{try{const n=await B(`/api/skills/unified-search?q=${encodeURIComponent(i)}&limit=20`);if(n.ok){const d=await n.json();c(d||[])}else c([])}catch{c([])}finally{k(!1)}},300),()=>{x.current&&clearTimeout(x.current)}}},[i,s]);const E=[...u.filter(n=>!f.includes(n.name)).filter(n=>!i||n.name.toLowerCase().includes(i.toLowerCase())).map(n=>({name:n.name,description:n.description||"",category:n.category||"",trigger:n.trigger||"",source:"db",kind:n.kind||"custom_tool",id:n.id})),...h.filter(n=>!f.includes(n.name)).filter(n=>b==="all"||b==="marketplace"&&n.source==="filesystem"||b==="mine"&&n.source==="db").filter(n=>!u.find(d=>d.name===n.name))];function R(n){n&&!f.includes(n)&&o({skills:[...f,n]}),r(!1),m(""),c([])}function I(n){o({skills:f.filter(d=>d!==n)})}const q=[{key:"all",label:a.agentConfig.allSkills||"All"},{key:"marketplace",label:a.agentConfig.marketplace},{key:"mine",label:a.agentConfig.mySkills||"Mine"}];return e.jsx(w,{title:a.agentConfig.skills,desc:a.agentConfig.skillsDesc,icon:P,children:e.jsxs("div",{className:"space-y-3",children:[e.jsxs("div",{className:"flex flex-wrap gap-2",children:[f.map(n=>{const d=u.find(N=>N.name===n),y=!d;return e.jsxs("span",{className:"inline-flex max-w-full items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2.5 py-1.5 text-xs text-foreground",children:[y?e.jsx(_,{className:"h-3 w-3 shrink-0 text-amber-600"}):e.jsx(P,{className:"h-3 w-3 shrink-0 text-primary"}),e.jsx("span",{className:"min-w-0 break-all",children:n}),(d==null?void 0:d.kind)==="system_skill"&&e.jsx("span",{className:"rounded bg-primary/10 px-1 py-0.5 text-[10px] text-primary",children:a.agentConfig.native}),y&&e.jsx("span",{className:"rounded bg-amber-500/10 px-1 py-0.5 text-[10px] text-amber-600",children:a.agentConfig.marketplace}),e.jsx("button",{onClick:()=>I(n),className:"text-muted-foreground hover:text-destructive",children:e.jsx(A,{className:"h-3 w-3"})})]},n)}),f.length===0&&e.jsx("span",{className:"text-xs text-muted-foreground",children:a.agentConfig.noSkills})]}),s?e.jsxs("div",{className:"rounded-lg border border-border bg-background p-2",children:[e.jsxs("div",{className:"mb-2 flex items-center gap-2",children:[e.jsx(U,{className:"h-3.5 w-3.5 text-muted-foreground"}),e.jsx("input",{value:i,onChange:n=>m(n.target.value),placeholder:a.agentConfig.searchSkill||"Search skills...",className:"flex-1 bg-transparent text-xs focus:outline-none",autoFocus:!0}),g&&e.jsx(X,{className:"h-3 w-3 animate-spin text-muted-foreground"}),e.jsx("button",{onClick:()=>r(!1),className:"text-muted-foreground hover:text-foreground",children:e.jsx(A,{className:"h-3.5 w-3.5"})})]}),e.jsx("div",{className:"mb-2 flex gap-1",children:q.map(n=>e.jsx("button",{onClick:()=>L(n.key),className:`rounded-md px-2 py-1 text-[11px] transition-colors ${b===n.key?"bg-primary/10 text-primary":"text-muted-foreground hover:text-foreground"}`,children:n.label},n.key))}),e.jsx("div",{className:"max-h-56 space-y-1 overflow-y-auto",children:E.length===0&&!g?e.jsx("p",{className:"px-2 py-3 text-center text-xs text-muted-foreground",children:i?a.agentConfig.noAvailableSkills||"No skills found":a.agentConfig.searchSkill||"Type to search marketplace skills..."}):E.map((n,d)=>e.jsxs("button",{onClick:()=>R(n.name),className:"flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary",children:[e.jsx(M,{className:"h-3 w-3 shrink-0 text-muted-foreground"}),e.jsxs("div",{className:"flex-1 min-w-0",children:[e.jsxs("div",{className:"flex items-center gap-1",children:[e.jsx("span",{className:"text-foreground truncate",children:n.name}),n.source==="filesystem"?e.jsx(_,{className:"h-2.5 w-2.5 shrink-0 text-amber-600"}):e.jsx(W,{className:"h-2.5 w-2.5 shrink-0 text-primary"})]}),n.description&&e.jsx("p",{className:"truncate text-[10px] text-muted-foreground",children:n.description})]}),n.category&&e.jsx("span",{className:"shrink-0 rounded bg-secondary px-1 py-0.5 text-[10px] text-muted-foreground",children:n.category})]},n.id||`${n.name}-${d}`))})]}):e.jsxs("button",{onClick:()=>r(!0),className:"inline-flex items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-1.5 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground",children:[e.jsx(M,{className:"h-3.5 w-3.5"})," ",a.agentConfig.addSkill]})]})})}export{le as E,me as P,ce as R,he as S,de as W,ne as c,pe as l,ue as n,Q as u};
