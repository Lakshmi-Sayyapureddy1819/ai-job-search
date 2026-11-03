import { access } from "node:fs/promises"

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context)
  } catch (error) {
    if (error?.code !== "ERR_MODULE_NOT_FOUND" || !specifier.endsWith(".js")) throw error
    const typescriptSpecifier = `${specifier.slice(0, -3)}.ts`
    const candidate = new URL(typescriptSpecifier, context.parentURL)
    await access(candidate)
    return nextResolve(typescriptSpecifier, context)
  }
}
