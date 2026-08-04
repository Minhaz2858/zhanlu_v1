import { useState, useEffect } from 'react';
import { getScreenTier, TIER_SETTINGS } from '@/lib/screen-config';

/**
 * useScreenSize — 桌面屏幕档位 Hook。
 *
 * 监听 window resize（requestAnimationFrame 节流，约 16ms 合并一次），
 * 返回当前档位及其统一配置：
 *
 *   { tier: 'compact'|'standard'|'wide'|'ultra', settings: TIER_SETTINGS[tier] }
 *
 * 遵循 use-mobile.jsx 的 useEffect 清理模式（removeEventListener +
 * cancelAnimationFrame）。仅在档位变化时触发重渲染，同档位内的 resize
 * 不更新 state。
 */
export function useScreenSize() {
  const [size, setSize] = useState(() => {
    const width = typeof window !== 'undefined' ? window.innerWidth : 1920;
    const tier = getScreenTier(width);
    return { tier, settings: TIER_SETTINGS[tier] };
  });

  useEffect(() => {
    let rafId = null;

    const update = () => {
      const tier = getScreenTier(window.innerWidth);
      setSize((prev) =>
        prev.tier === tier ? prev : { tier, settings: TIER_SETTINGS[tier] },
      );
    };

    const onResize = () => {
      if (rafId != null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        update();
      });
    };

    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      if (rafId != null) cancelAnimationFrame(rafId);
    };
  }, []);

  return size;
}
