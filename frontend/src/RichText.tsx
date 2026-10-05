import type { ReactNode } from "react";

// Renders the small Markdown subset the assistant may use: **bold** and "- " bullet lists.
// Everything else stays literal text; React escapes it, so no HTML from a reply is ever injected.

function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*\n]+\*\*)/g);
  return parts.map((part, index) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4
      ? <strong key={`${keyPrefix}-${index}`}>{part.slice(2, -2)}</strong>
      : part,
  );
}

export function RichText({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let bullets: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      const key = `p-${blocks.length}`;
      blocks.push(<p key={key}>{inline(paragraph.join("\n"), key)}</p>);
      paragraph = [];
    }
  };
  const flushBullets = () => {
    if (bullets.length) {
      const key = `ul-${blocks.length}`;
      blocks.push(<ul key={key}>{bullets.map((item, index) => <li key={`${key}-${index}`}>{inline(item, `${key}-${index}`)}</li>)}</ul>);
      bullets = [];
    }
  };

  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*(?:[-*•])\s+(.*)$/);
    if (bullet) {
      flushParagraph();
      bullets.push(bullet[1]);
    } else if (!line.trim()) {
      flushParagraph();
      flushBullets();
    } else {
      flushBullets();
      paragraph.push(line);
    }
  }
  flushParagraph();
  flushBullets();
  return <div className="message-text">{blocks}</div>;
}
