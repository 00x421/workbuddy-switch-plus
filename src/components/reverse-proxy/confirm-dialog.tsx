import { useCallback, useState } from "react";
import { toast } from "sonner";

import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Banner } from "./rp-shared";

export type ConfirmRequest = {
  title: string;
  description?: string;
  confirmLabel?: string;
  danger?: boolean;
  run: () => Promise<unknown>;
};

/** 破坏性/需确认操作的统一弹窗（对应内置后台的 confirm-dialog）。 */
export function ConfirmDialog({ request, onClose, onDone }: {
  request: ConfirmRequest | null;
  onClose: () => void;
  onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <AlertDialog open={Boolean(request)} onOpenChange={(open) => { if (!open && !busy) { setError(""); onClose(); } }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{request?.title}</AlertDialogTitle>
          {request?.description ? <AlertDialogDescription>{request.description}</AlertDialogDescription> : null}
        </AlertDialogHeader>
        {error ? <Banner tone="bad">{error}</Banner> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            className={request?.danger ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
            onClick={async (event) => {
              event.preventDefault();
              if (!request) return;
              setBusy(true);
              setError("");
              try {
                await request.run();
                onClose();
                onDone?.();
              } catch (cause) {
                setError(cause instanceof Error ? cause.message : String(cause));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "处理中…" : request?.confirmLabel ?? "确认"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** 一个极简的确认弹窗状态机，供各面板复用。 */
export function useConfirm() {
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const ask = useCallback((next: ConfirmRequest) => setRequest(next), []);
  const close = useCallback(() => setRequest(null), []);
  const reportError = useCallback((cause: unknown) => {
    toast.error(cause instanceof Error ? cause.message : String(cause));
  }, []);
  return { request, ask, close, reportError };
}
