import type { Metadata } from "next";
import { Archivo, IBM_Plex_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const archivo = Archivo({
  variable: "--font-archivo",
  subsets: ["latin"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("host") ?? "localhost:3000";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ?? (host.startsWith("localhost") ? "http" : "https");
  const metadataBase = new URL(`${protocol}://${host}`);

  return {
    metadataBase,
    title: "PDF Bridge documentation",
    description:
      "Internal role guides and technical reference for the PDF Bridge proof of concept.",
    openGraph: {
      title: "PDF Bridge documentation",
      description: "Role guides, lifecycle, contracts, operations, and code reference.",
      type: "website",
    },
    twitter: {
      card: "summary",
      title: "PDF Bridge documentation",
      description: "Role guides, lifecycle, contracts, operations, and code reference.",
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={`${archivo.variable} ${plexMono.variable}`}>{children}</body>
    </html>
  );
}
