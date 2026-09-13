import { createContext, useContext, useEffect, useState } from 'react';
import { fetchNodeTypes } from '@/api/workflows';

// In-memory cache of the backend node-type registry
// ({ connectionTypes: {...}, nodeTypes: [...] }). Fetched once per session;
// mirrors n8n's shared node-type registry between editor and runtime.
let cache = null;
let inflight = null;

export function getNodeTypeRegistry() {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = fetchNodeTypes()
      .then((d) => {
        cache = d;
        return d;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function nodeTypeOf(typeName) {
  if (!cache) return null;
  return cache.nodeTypes.find((t) => t.name === typeName) || null;
}

export function connectionMeta(typeName) {
  if (!cache) return { label: typeName, color: '#8b5cf6' };
  return cache.connectionTypes[typeName] || { label: typeName, color: '#8b5cf6' };
}

// ---------------------------------------------------------------------------
// React context so the canvas node renderer sees the registry once loaded.
// ---------------------------------------------------------------------------
export const NodeRegistryContext = createContext(null);

export function NodeRegistryProvider({ children }) {
  const [registry, setRegistry] = useState(null);
  useEffect(() => {
    let alive = true;
    getNodeTypeRegistry()
      .then((d) => alive && setRegistry(d))
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  return (
    <NodeRegistryContext.Provider value={registry}>
      {children}
    </NodeRegistryContext.Provider>
  );
}

export function useNodeRegistry() {
  return useContext(NodeRegistryContext);
}
