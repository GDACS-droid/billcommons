import type { ReactNode } from "react";

export default function PageHeader({
  eyebrow,
  title,
  description,
  className,
}: {
  eyebrow?: string;
  title: ReactNode;
  description?: ReactNode;
  className?: string;
}) {
  return (
    <header className={`page-header ${className ?? ""}`}>
      {eyebrow ? <p className="page-eyebrow">{eyebrow}</p> : null}
      <h1 className="page-title">{title}</h1>
      {description ? <div className="page-lead">{description}</div> : null}
    </header>
  );
}
