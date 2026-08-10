"use client";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <main className="centered error-page"><h1>Scribe could not open this page</h1><p>Your local project data was not changed.</p><button className="primary-button" onClick={reset}>Try again</button></main>;
}
