// Friendly product labels for the market dashboard.
// Mirrors backend/app/models/market_dashboard.py::PRODUCT_LABELS so the
// frontend can display human-readable product names without round-tripping
// the backend for every render.

export const PRODUCT_LABELS = {
  crude_oil:     { label_en: "Crude Oil (Brent)",  label_zh: "原油(布伦特)", group: "upstream" },
  naphtha:       { label_en: "Japanese Naphtha",   label_zh: "日本石脑油",   group: "upstream" },
  cracked_c5:    { label_en: "Cracked C5",         label_zh: "裂解碳五",     group: "upstream" },
  cracked_c9:    { label_en: "Cracked C9",         label_zh: "裂解碳九",     group: "upstream" },
  mixed_c5:      { label_en: "Yangzi Cracked C5",  label_zh: "扬子裂解C5",   group: "upstream" },
  isoprene:      { label_en: "Isoprene",           label_zh: "异戊二烯",     group: "midstream" },
  piperylene:    { label_en: "Piperylene",         label_zh: "间戊二烯",     group: "midstream" },
  dcpd:          { label_en: "DCPD",               label_zh: "双环戊二烯",   group: "midstream" },
  raffinate_c5:  { label_en: "Raffinate C5",       label_zh: "抽余C5",       group: "midstream" },
  sis:           { label_en: "SIS Rubber",         label_zh: "SIS橡胶",      group: "downstream" },
  sis_rubber:    { label_en: "SIS Rubber",         label_zh: "SIS橡胶",      group: "downstream" },
  c5_resin:      { label_en: "C5 Petroleum Resin", label_zh: "C5石油树脂",   group: "downstream" },
  blowing_agent: { label_en: "Blowing Agent",      label_zh: "发泡剂",       group: "downstream" },
  styrene:       { label_en: "Styrene",            label_zh: "苯乙烯",       group: "downstream" },
};

// Product group membership — mirrors backend PRODUCT_GROUP_MAP.
export const PRODUCT_GROUPS = {
  overview:    [], // empty = all products
  upstream:   ["crude_oil", "naphtha", "cracked_c5", "cracked_c9"],
  midstream:  ["isoprene", "piperylene", "dcpd", "raffinate_c5"],
  downstream: ["sis", "c5_resin", "blowing_agent", "styrene"],
};

// Products that have a forecast in the v_forecast_price view.
// Mirrors backend MATERIAL_FORECAST_IDS keys.
export const PRODUCTS_WITH_FORECAST = ["cracked_c5", "cracked_c9"];
