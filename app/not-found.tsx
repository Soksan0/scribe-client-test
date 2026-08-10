import Link from "next/link";

export default function NotFound() {
  return <main className="centered"><h1>Page not found</h1><p>This Scribe page does not exist.</p><Link className="primary-button" href="/">Return to projects</Link></main>;
}
