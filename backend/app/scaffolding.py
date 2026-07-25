"""Project scaffolding: the starter files written into a new workspace.

Almost entirely template literals for the three supported stack profiles
(Next.js web, Fastify API, FastAPI API) plus the stack sniffing that picks
between them. Bulky but inert — nothing here reaches back into the app, which
is why it was the first thing worth lifting out of `main.py`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .store import ROOT_DIR
from .tasks import task_title

STACK_PROFILE_VALUES = {"smart", "next-web", "fastify-api", "fastapi-api", "none"}


def clean_stack_profile(stack_profile: str | None) -> str:
    value = str(stack_profile or "smart").strip().lower()
    return value if value in STACK_PROFILE_VALUES else "smart"


def infer_stack_profile(spec: str, stack_profile: str | None = "smart") -> str:
    requested = clean_stack_profile(stack_profile)
    if requested != "smart":
        return requested

    text = spec.lower()
    python_terms = ("python", "fastapi", "ml", "machine learning", "data science", "pandas", "numpy", "notebook")
    node_api_terms = ("api-only", "api only", "backend api", "rest api", "webhook", "microservice", "fastify")
    frontend_terms = ("next", "react", "frontend", "ui", "dashboard", "page", "website", "web app", "tailwind")
    if any(term in text for term in python_terms):
        return "fastapi-api"
    if any(term in text for term in node_api_terms) and not any(term in text for term in frontend_terms):
        return "fastify-api"
    return "next-web"


def write_scaffold_file(project_path: Path, relative_path: str, content: str, *, overwrite: bool = False) -> None:
    target = project_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not target.exists():
        target.write_text(content.lstrip("\n"), encoding="utf-8")


def write_next_web_scaffold(project_path: Path, spec: str) -> None:
    package_json = {
        "name": project_path.name,
        "version": "0.1.0",
        "private": True,
        "type": "module",
        "scripts": {
            "dev": "next dev",
            "build": "next build",
            "start": "next start",
            "typecheck": "tsc --noEmit",
            "lint": "eslint . --max-warnings=0",
            "check": "npm run typecheck && npm run lint",
        },
        "dependencies": {
            "@radix-ui/react-slot": "^1.1.0",
            "class-variance-authority": "^0.7.1",
            "clsx": "^2.1.1",
            "lucide-react": "^0.468.0",
            "next": "^15.3.0",
            "react": "^19.0.0",
            "react-dom": "^19.0.0",
            "tailwind-merge": "^2.5.5",
            "tailwindcss-animate": "^1.0.7",
        },
        "devDependencies": {
            "@eslint/eslintrc": "^3.2.0",
            "@types/node": "^22.10.0",
            "@types/react": "^19.0.0",
            "@types/react-dom": "^19.0.0",
            "eslint": "^9.15.0",
            "eslint-config-next": "^15.3.0",
            "autoprefixer": "^10.4.20",
            "postcss": "^8.4.49",
            "tailwindcss": "^3.4.17",
            "typescript": "^5.7.2",
        },
    }
    write_scaffold_file(project_path, "package.json", json.dumps(package_json, indent=2) + "\n")
    write_scaffold_file(
        project_path,
        "tsconfig.json",
        """{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "es2022"],
    "allowJs": false,
    "skipLibCheck": true,
    "strict": true,
    "noImplicitAny": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }]
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
""",
    )
    write_scaffold_file(project_path, "next.config.ts", "import type { NextConfig } from \"next\";\n\nconst nextConfig: NextConfig = {};\n\nexport default nextConfig;\n")
    write_scaffold_file(project_path, "postcss.config.mjs", "export default {\n  plugins: {\n    tailwindcss: {},\n    autoprefixer: {},\n  },\n};\n")
    write_scaffold_file(
        project_path,
        "tailwind.config.ts",
        """import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
      },
    },
  },
  plugins: [animate],
};

export default config;
""",
    )
    write_scaffold_file(
        project_path,
        "eslint.config.mjs",
        """import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({
  baseDirectory: dirname(fileURLToPath(import.meta.url)),
});

export default [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
];
""",
    )
    write_scaffold_file(project_path, ".gitignore", "node_modules\n.next\n.env*.local\ndist\ncoverage\n")
    write_scaffold_file(project_path, "next-env.d.ts", "/// <reference types=\"next\" />\n/// <reference types=\"next/image-types/global\" />\n")
    write_scaffold_file(
        project_path,
        "lib/utils.ts",
        """import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
""",
    )
    write_scaffold_file(
        project_path,
        "components/ui/button.tsx",
        """import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex h-10 items-center justify-center gap-2 rounded-md border border-transparent px-4 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:opacity-90",
        outline: "border-border bg-transparent hover:bg-foreground/5",
        ghost: "hover:bg-foreground/5",
      },
      size: {
        default: "h-10 px-4",
        sm: "h-8 px-3 text-xs",
        icon: "size-10 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />;
  },
);

Button.displayName = "Button";
""",
    )
    write_scaffold_file(
        project_path,
        "app/layout.tsx",
        """import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dualith App",
  description: "A modern strict TypeScript app scaffolded by Dualith.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
""",
    )
    title = task_title(spec) if spec.strip() else "Modern App"
    title_literal = json.dumps(title)
    write_scaffold_file(
        project_path,
        "app/page.tsx",
        f"""import {{ ArrowRight }} from "lucide-react";

import {{ Button }} from "@/components/ui/button";

export default function HomePage() {{
  return (
    <main className="min-h-screen bg-background text-foreground">
      <section className="mx-auto flex min-h-screen w-full max-w-5xl flex-col justify-center gap-8 px-6 py-12">
        <div className="max-w-3xl space-y-4">
          <p className="text-sm font-medium uppercase tracking-wide text-foreground/60">Dualith starter</p>
          <h1 className="text-4xl font-semibold tracking-normal sm:text-6xl">{{{title_literal}}}</h1>
          <p className="max-w-2xl text-lg leading-8 text-foreground/70">
            A Next.js App Router, React, strict TypeScript, Tailwind, and shadcn-compatible foundation is ready.
          </p>
        </div>
        <div>
          <Button>
            Start building
            <ArrowRight className="size-4" aria-hidden="true" />
          </Button>
        </div>
      </section>
    </main>
  );
}}
""",
    )
    write_scaffold_file(
        project_path,
        "app/globals.css",
        """@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --background: 210 40% 98%;
  --foreground: 222 47% 11%;
  --border: 214 32% 91%;
  --primary: 222 47% 11%;
  --primary-foreground: 210 40% 98%;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: hsl(var(--background));
  color: hsl(var(--foreground));
  font-family: Arial, Helvetica, sans-serif;
}
""",
    )


def write_fastify_scaffold(project_path: Path) -> None:
    package_json = {
        "name": project_path.name,
        "version": "0.1.0",
        "private": True,
        "type": "module",
        "scripts": {
            "dev": "tsx watch src/server.ts",
            "build": "tsc -p tsconfig.json",
            "start": "node dist/server.js",
            "typecheck": "tsc --noEmit",
            "check": "npm run typecheck",
        },
        "dependencies": {
            "@fastify/cors": "^10.0.0",
            "dotenv": "^16.4.7",
            "fastify": "^5.1.0",
            "zod": "^3.24.1",
        },
        "devDependencies": {
            "@types/node": "^22.10.0",
            "tsx": "^4.19.2",
            "typescript": "^5.7.2",
        },
    }
    write_scaffold_file(project_path, "package.json", json.dumps(package_json, indent=2) + "\n")
    write_scaffold_file(
        project_path,
        "tsconfig.json",
        """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "noImplicitAny": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["src/**/*.ts"]
}
""",
    )
    write_scaffold_file(
        project_path,
        "src/server.ts",
        """import Fastify, { type FastifyInstance } from "fastify";
import cors from "@fastify/cors";
import "dotenv/config";

const port = Number(process.env.PORT ?? 3000);
const host = process.env.HOST ?? "0.0.0.0";

export async function buildServer(): Promise<FastifyInstance> {
  const app = Fastify({ logger: true });
  await app.register(cors, { origin: true });

  app.get("/health", async () => ({ ok: true }));

  return app;
}

const app = await buildServer();
await app.listen({ port, host });
""",
    )
    write_scaffold_file(project_path, ".env.example", "PORT=3000\nHOST=0.0.0.0\n")
    write_scaffold_file(project_path, ".gitignore", "node_modules\ndist\n.env\ncoverage\n")


def write_fastapi_scaffold(project_path: Path) -> None:
    write_scaffold_file(
        project_path,
        "pyproject.toml",
        f"""[project]
name = "{project_path.name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.32.0",
  "pydantic>=2.10.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.3.0", "ruff>=0.8.0"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
    )
    write_scaffold_file(
        project_path,
        "app/main.py",
        """from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


app = FastAPI(title="Dualith API")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True)
""",
    )
    write_scaffold_file(
        project_path,
        "tests/test_health.py",
        """from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
""",
    )
    write_scaffold_file(project_path, ".env.example", "PORT=8000\n")
    write_scaffold_file(project_path, ".gitignore", ".venv\n__pycache__\n.pytest_cache\n.ruff_cache\n.env\n")


def scaffold_project_stack(project_path: Path, spec: str, stack_profile: str | None) -> str:
    selected = infer_stack_profile(spec, stack_profile)
    if selected == "next-web":
        write_next_web_scaffold(project_path, spec)
    elif selected == "fastify-api":
        write_fastify_scaffold(project_path)
    elif selected == "fastapi-api":
        write_fastapi_scaffold(project_path)
    return selected


def copy_impeccable_skill(project_path: Path) -> None:
    for harness_dir in (".agents", ".claude"):
        source = ROOT_DIR / harness_dir / "skills" / "impeccable"
        dest = project_path / harness_dir / "skills" / "impeccable"
        if source.exists() and not dest.exists():
            shutil.copytree(source, dest)
