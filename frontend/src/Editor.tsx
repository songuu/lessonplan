import { useEffect, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import {
  Bold,
  Italic,
  Heading2,
  List,
  ListOrdered,
  Quote,
  Undo2,
  Redo2,
  Code2,
  Eye,
  TextCursorInput,
} from "lucide-react";
import { locateSelection, renderMarkdown } from "./content";
export type Selection = { start: number; end: number; text: string };
export function RichEditor({
  value,
  onChange,
  onSelection,
  readonly = false,
}: {
  value: string;
  onChange: (content: string) => void;
  onSelection?: (range: Selection | null) => void;
  readonly?: boolean;
}) {
  const [source, setSource] = useState(false);
  const [preview, setPreview] = useState(false);
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const editor = useEditor({
    extensions: [StarterKit, Markdown],
    content: value,
    contentType: "markdown",
    editable: !readonly,
    editorProps: {
      attributes: {
        class: "rich-editor",
        role: "textbox",
        "aria-label": "教案模块正文",
        "aria-multiline": "true",
      },
    },
    onUpdate: ({ editor }) => changeRef.current(editor.getMarkdown()),
    onSelectionUpdate: ({ editor }) => {
      const { from, to } = editor.state.selection;
      const text = editor.state.doc.textBetween(from, to, "\n");
      onSelection?.(locateSelection(editor.getMarkdown(), text));
    },
  });
  useEffect(() => {
    if (editor && editor.getMarkdown() !== value)
      editor.commands.setContent(value, {
        contentType: "markdown",
        emitUpdate: false,
      });
  }, [editor, value]);
  useEffect(() => {
    editor?.setEditable(!readonly);
  }, [editor, readonly]);
  const commands = [
    {
      label: "粗体",
      icon: Bold,
      action: () => editor?.chain().focus().toggleBold().run(),
      active: editor?.isActive("bold"),
    },
    {
      label: "斜体",
      icon: Italic,
      action: () => editor?.chain().focus().toggleItalic().run(),
      active: editor?.isActive("italic"),
    },
    {
      label: "二级标题",
      icon: Heading2,
      action: () => editor?.chain().focus().toggleHeading({ level: 2 }).run(),
      active: editor?.isActive("heading"),
    },
    {
      label: "无序列表",
      icon: List,
      action: () => editor?.chain().focus().toggleBulletList().run(),
    },
    {
      label: "有序列表",
      icon: ListOrdered,
      action: () => editor?.chain().focus().toggleOrderedList().run(),
    },
    {
      label: "引用",
      icon: Quote,
      action: () => editor?.chain().focus().toggleBlockquote().run(),
    },
    {
      label: "撤销",
      icon: Undo2,
      action: () => editor?.chain().focus().undo().run(),
    },
    {
      label: "重做",
      icon: Redo2,
      action: () => editor?.chain().focus().redo().run(),
    },
  ];
  return (
    <div className="editor-shell">
      <div className="editor-toolbar" role="toolbar" aria-label="正文格式">
        {!source &&
          !preview &&
          commands.map((c) => (
            <button
              key={c.label}
              type="button"
              aria-label={c.label}
              title={c.label}
              className={`icon-button ${c.active ? "active" : ""}`}
              onClick={c.action}
              disabled={readonly}
            >
              <c.icon size={17} />
            </button>
          ))}
        <span className="toolbar-spacer" />
        <button
          type="button"
          className={`toolbar-mode ${source ? "active" : ""}`}
          onClick={() => {
            setSource(!source);
            setPreview(false);
            onSelection?.(null);
          }}
        >
          <Code2 size={15} /> 源码
        </button>
        <button
          type="button"
          className={`toolbar-mode ${preview ? "active" : ""}`}
          onClick={() => {
            setPreview(!preview);
            onSelection?.(null);
          }}
        >
          <Eye size={15} /> 预览
        </button>
      </div>
      {preview ? (
        <div
          className="markdown rendered"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(value) }}
        />
      ) : source ? (
        <textarea
          className="source-editor"
          aria-label="Markdown 正文"
          value={value}
          readOnly={readonly}
          onChange={(e) => onChange(e.target.value)}
          onSelect={(e) => {
            const t = e.currentTarget;
            onSelection?.(
              t.selectionEnd > t.selectionStart
                ? {
                    start: t.selectionStart,
                    end: t.selectionEnd,
                    text: value.slice(t.selectionStart, t.selectionEnd),
                  }
                : null,
            );
          }}
        />
      ) : (
        <EditorContent editor={editor} />
      )}
      <footer className="editor-footer">
        <span>
          <TextCursorInput size={13} /> {value.length.toLocaleString()} 字符
        </span>
        <span>支持 Markdown · 数学公式 · 化学式 · IPA</span>
      </footer>
    </div>
  );
}
