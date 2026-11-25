import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createCrashEventsSource, getCrash } from "../../api/client";
import type { CrashDetailRead } from "../../api/types";
import { isCrashInFlight } from "./utils";

export function useCrashDetailLive(crashId: string | undefined) {
  const queryClient = useQueryClient();
  const [sseConnected, setSseConnected] = useState(false);
  const [sseStage, setSseStage] = useState<string | null>(null);
  const [sseError, setSseError] = useState<string | null>(null);

  useEffect(() => {
    setSseConnected(false);
    setSseStage(null);
    setSseError(null);
  }, [crashId]);

  const crashQuery = useQuery({
    queryKey: ["crash", crashId],
    queryFn: () => getCrash(crashId!),
    enabled: Boolean(crashId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return isCrashInFlight(status) && !sseConnected ? 3000 : false;
    },
  });

  const crash = crashQuery.data;
  const inFlight = isCrashInFlight(crash?.status);

  // Live SSE subscription while crash is PENDING / PARSING / TRIAGING
  useEffect(() => {
    if (!crashId || !inFlight) {
      setSseConnected(false);
      return;
    }

    const close = createCrashEventsSource(crashId, {
      onOpen: () => {
        setSseConnected(true);
        setSseError(null);
      },
      onStatus: (event) => {
        setSseStage(event.stage ?? null);
        setSseError(event.error ?? null);

        let statusChanged = false;
        queryClient.setQueryData<CrashDetailRead>(
          ["crash", crashId],
          (prev) => {
            if (!prev) {
              return prev;
            }
            if (prev.status !== event.status) {
              statusChanged = true;
            }
            return {
              ...prev,
              status: event.status,
              ai_verdict: event.ai_verdict ?? prev.ai_verdict,
            };
          },
        );

        const isTerminal =
          event.status === "ANALYZED" || event.status === "FAILED";
        if (statusChanged || isTerminal) {
          void queryClient.invalidateQueries({ queryKey: ["crash", crashId] });
        }
        if (isTerminal) {
          void queryClient.invalidateQueries({ queryKey: ["crashes"] });
          void queryClient.invalidateQueries({ queryKey: ["issues"] });
          void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        }
      },
      onError: () => {
        setSseConnected(false);
      },
    });

    return () => {
      close();
      setSseConnected(false);
    };
  }, [crashId, inFlight, queryClient]);

  const resetSseState = useCallback(() => {
    setSseError(null);
    setSseStage(null);
  }, []);

  return {
    crashQuery,
    crash,
    inFlight,
    sseConnected,
    sseStage,
    sseError,
    resetSseState,
  };
}
