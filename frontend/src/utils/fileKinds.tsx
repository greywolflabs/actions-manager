import type React from "react";
import { FileText, Users, type LucideIcon } from "lucide-react";
import CodeownersManager from "../components/CodeownersManager";

/** What a Managed Files project's page hands its kind's editor. */
export type FileKindEditorProps = React.ComponentProps<typeof CodeownersManager>;

/**
 * The frontend half of a Managed Files kind (#2367): what the server's
 * definition (GET /api/managed-files) can't carry. The label and paths come
 * from the server. A kind without an entry here still renders in the
 * section, with the generic file icon.
 */
export interface FileKindUi {
  icon: LucideIcon;
  /** Edits the project's file on its page. */
  Editor: React.ComponentType<FileKindEditorProps>;
}

export const FILE_KIND_UI: Record<string, FileKindUi> = {
  codeowners: { icon: Users, Editor: CodeownersManager },
};

export const fileKindIcon = (kind: string | null | undefined): LucideIcon =>
  (kind ? FILE_KIND_UI[kind]?.icon : undefined) ?? FileText;
