import { createContext, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';

import { ApiError } from '../api/client';
import { fetchSources } from '../api/endpoints';
import type { Source, Topic } from '../api/types';

export interface CatalogueValue {
  status: 'loading' | 'ready' | 'error';
  sources: Source[];
  topics: Topic[];
  sourceBySlug: Map<string, Source>;
  topicBySlug: Map<string, Topic>;
  error: ApiError | null;
  reload: () => void;
}

export const CatalogueContext = createContext<CatalogueValue | null>(null);

export function CatalogueProvider({ children }: { children: ReactNode }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [topics, setTopics] = useState<Topic[]>([]);
  const [status, setStatus] = useState<CatalogueValue['status']>('loading');
  const [error, setError] = useState<ApiError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    fetchSources(controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return;
        setSources(response.sources);
        setTopics(response.topics);
        setError(null);
        setStatus('ready');
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
        setStatus('error');
      });
    return () => {
      controller.abort();
    };
  }, [reloadToken]);

  const reload = useCallback(() => {
    setStatus('loading');
    setReloadToken((value) => value + 1);
  }, []);

  const value = useMemo<CatalogueValue>(
    () => ({
      status,
      sources,
      topics,
      sourceBySlug: new Map(sources.map((source) => [source.slug, source])),
      topicBySlug: new Map(topics.map((topic) => [topic.slug, topic])),
      error,
      reload,
    }),
    [status, sources, topics, error, reload],
  );

  return <CatalogueContext value={value}>{children}</CatalogueContext>;
}
