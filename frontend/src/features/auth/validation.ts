import { z } from 'zod'

const email = z.email('Enter a valid email address.').max(254, 'Email is too long.')

export const loginSchema = z.object({
  email,
  password: z.string().min(1, 'Enter your password.').max(128, 'Password is too long.'),
})

export const registrationSchema = z.object({
  display_name: z.string().trim().min(1, 'Enter your name.').max(100, 'Name is too long.'),
  email,
  password: z.string().min(15, 'Use at least 15 characters.').max(128, 'Password is too long.'),
  confirm_password: z.string().min(1, 'Confirm your password.'),
}).refine((data) => data.password === data.confirm_password, {
  path: ['confirm_password'], message: 'Passwords do not match.',
})

export type LoginValues = z.infer<typeof loginSchema>
export type RegistrationValues = z.infer<typeof registrationSchema>
