/**
 * Emit a schema.org JSON-LD block.
 *
 * The payload is our own server-built object, never user input, but it is still
 * serialized with `<` escaped: a bill title containing "</script>" would
 * otherwise terminate the block early and inject markup into the page.
 */
export default function JsonLd({ data }: { data: unknown }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{
        __html: JSON.stringify(data).replace(/</g, "\\u003c"),
      }}
    />
  );
}
