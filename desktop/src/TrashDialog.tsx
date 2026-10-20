import { useEffect, useState } from "react";
import { Alert, Button, Group, Modal, ScrollArea, Stack, Text } from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";
import { message, rpc } from "./api";

export type TrashTarget = { label: string } & (
  | { kind: "assets"; library_id: string; ids: string[] }
  | { kind: "outputs"; project_id: string; ids: string[] }
  | { kind: "batch" | "job"; project_id: string; job_id: string }
);
type Plan = { paths: string[]; shared_folder: boolean };
type Result = { errors: { path: string; error: string }[]; deleted_job: boolean };

export default function TrashDialog({ target, onClose, onChanged }: {
  target: TrashTarget | null; onClose: () => void; onChanged: () => void;
}) {
  const [plan, setPlan] = useState<Plan>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setPlan(undefined);
    setError("");
    if (!target) return;
    let current = true;
    rpc<Plan>("trash.preview", { target }).then((value) => {
      if (current) setPlan(value);
    }).catch((reason) => { if (current) setError(message(reason)); });
    return () => { current = false; };
  }, [target, retry]);
  async function remove() {
    if (!target || busy || !plan) return;
    setBusy(true);
    setError("");
    try {
      const result = await rpc<Result>("trash.execute", { target });
      onChanged();
      if (result.errors.length) setError(result.errors.map((item) => `${item.path}：${item.error}`).join("\n"));
      else onClose();
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }
  return <Modal opened={!!target} onClose={() => !busy && onClose()} title="移到废纸篓？"
    centered size={560} closeOnClickOutside={false} closeOnEscape={!busy} withCloseButton={!busy}>
    <Stack gap="md">
      <Text size="sm" fw={600}>{target?.label}</Text>
      <Text size="sm">{target?.kind === "job"
        ? "将删除此任务，并把对应的成片文件夹或视频移到系统废纸篓。原始素材保留。"
        : target?.kind === "assets"
          ? "将从硬盘原位置移走所选素材，其他项目中对这些文件的引用也会失效。"
          : "将从硬盘原位置移走所选成片或文件夹，并从成片库移除。"}</Text>
      {plan?.shared_folder && <Text size="sm" c="dimmed">此旧任务使用共享目录，只移走本任务的视频，保留目录中的其他文件。</Text>}
      {plan && <ScrollArea.Autosize mah={180}><Stack gap={6}>
        {plan.paths.map((path) => <Text key={path} size="xs" className="trash-path">{path}</Text>)}
        {!plan.paths.length && <Text size="sm" c="dimmed">没有待移走的成片文件，将删除任务记录。</Text>}
      </Stack></ScrollArea.Autosize>}
      {!plan && !error && <Text size="sm" c="dimmed">正在检查文件与任务关联…</Text>}
      {error && <Alert color="red" title="未能全部完成"><Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{error}</Text>
        {!plan && <Button variant="subtle" size="xs" onClick={() => setRetry((value) => value + 1)}>重新检查</Button>}
      </Alert>}
      <Text size="xs" c="dimmed">文件可从系统废纸篓恢复。程序不会清空废纸篓。</Text>
      <Group justify="flex-end">
        <Button variant="default" disabled={busy} onClick={onClose}>取消</Button>
        <Button color="red" leftSection={<IconTrash size={16} />} disabled={!plan} loading={busy} onClick={remove}>
          {target?.kind === "job" ? "删除任务及成片" : "移到废纸篓"}
        </Button>
      </Group>
    </Stack>
  </Modal>;
}
