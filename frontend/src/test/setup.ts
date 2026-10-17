import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// jsdom does not implement browser scrolling.
window.scrollTo = () => {}

afterEach(() => cleanup())
