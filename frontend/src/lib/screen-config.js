/**
 * screen-config.js — 桌面分辨率统一配置中心。
 *
 * 战颅系统桌面分辨率自适应（1366×768 ~ 4K）。所有框架层尺寸参数集中
 * 在这里，按窗口宽度分档生效：
 *
 *   compact   < 1366px          小屏笔记本（1366×768）：侧边栏收窄、聊天侧面板收紧
 *   standard  1366–1919px       常规桌面（默认档，等价于改动前的固定布局）
 *   wide      1920–2559px       大屏（1080p/2K）：侧边栏加宽、内容区限宽
 *   ultra     ≥ 2560px          超宽屏（4K 等）：进一步加宽、内容区限宽
 *
 * 使用方式见 `hooks/useScreenSize.js`；业务页面不依赖本模块。
 */

export const SCREEN_TIERS = [
  { key: 'compact', min: 0, max: 1365 },
  { key: 'standard', min: 1366, max: 1919 },
  { key: 'wide', min: 1920, max: 2559 },
  { key: 'ultra', min: 2560, max: Infinity },
];

// 聊天页五个面板的默认/最小/最大尺寸（react-resizable-panels 百分比）。
// 改动前的现状：chat defaultSize=100 minSize=30；scheduled 30/22/65；
// artifact 30/20/75；preview 42/25/70；dashboard 42/25/70。
const PANELS_DEFAULT = {
  chat: { defaultSize: 100, minSize: 30, maxSize: 100 },
  scheduled: { defaultSize: 30, minSize: 22, maxSize: 65 },
  artifact: { defaultSize: 30, minSize: 20, maxSize: 75 },
  preview: { defaultSize: 42, minSize: 25, maxSize: 70 },
  dashboard: { defaultSize: 42, minSize: 25, maxSize: 70 },
};

export const TIER_SETTINGS = {
  compact: {
    sidebarWidth: 208, // px（比默认档 256 窄 48px，肉眼可辨）
    // 小屏本身不够宽，不设内容区上限，但收窄内容区左右留白
    contentMaxWidth: null,
    contentPadding: 12, // px
    panels: {
      // 提高聊天区最小占比，避免侧面板把对话挤没
      chat: { defaultSize: 100, minSize: 35, maxSize: 100 },
      scheduled: { defaultSize: 25, minSize: 18, maxSize: 55 },
      artifact: { defaultSize: 25, minSize: 15, maxSize: 60 },
      preview: { defaultSize: 35, minSize: 20, maxSize: 60 },
      dashboard: { defaultSize: 35, minSize: 20, maxSize: 60 },
    },
  },
  standard: {
    sidebarWidth: 256, // px
    contentMaxWidth: null,
    contentPadding: 0, // px
    panels: PANELS_DEFAULT,
  },
  wide: {
    sidebarWidth: 288, // px
    // 大屏内容区上限，避免页面过宽、文本行过长
    contentMaxWidth: 1600, // px
    panels: {
      chat: { defaultSize: 100, minSize: 30, maxSize: 100 },
      scheduled: { defaultSize: 30, minSize: 22, maxSize: 65 },
      artifact: { defaultSize: 32, minSize: 20, maxSize: 75 },
      preview: { defaultSize: 44, minSize: 25, maxSize: 70 },
      dashboard: { defaultSize: 44, minSize: 25, maxSize: 70 },
    },
  },
  ultra: {
    sidebarWidth: 320, // px
    contentMaxWidth: 1920, // px
    contentPadding: 0, // px
    panels: {
      chat: { defaultSize: 100, minSize: 30, maxSize: 100 },
      scheduled: { defaultSize: 32, minSize: 22, maxSize: 65 },
      artifact: { defaultSize: 34, minSize: 20, maxSize: 75 },
      preview: { defaultSize: 46, minSize: 25, maxSize: 70 },
      dashboard: { defaultSize: 46, minSize: 25, maxSize: 70 },
    },
  },
};

/**
 * 根据窗口宽度返回档位 key（'compact' | 'standard' | 'wide' | 'ultra'）。
 * 找不到匹配时回退 'standard'，避免边界值（如 1919.5px）导致 undefined。
 */
export function getScreenTier(width) {
  const tier = SCREEN_TIERS.find((t) => width >= t.min && width <= t.max);
  return tier ? tier.key : 'standard';
}
