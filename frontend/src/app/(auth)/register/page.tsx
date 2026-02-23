'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useForm } from 'react-hook-form';
import { Eye, EyeOff, Mail, Lock, User, Building2, Cpu } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Input }  from '@/components/ui/Input';
import { useToast } from '@/hooks/useToast';
import { useRouter } from 'next/navigation';
import { ROUTES } from '@/lib/constants';
import { apiPost } from '@/lib/api';

interface RegisterFormData {
  name:            string;
  email:           string;
  organization:    string;
  password:        string;
  confirmPassword: string;
}

export default function RegisterPage() {
  const router = useRouter();
  const { success, error: toastError } = useToast();
  const [showPassword, setShowPassword] = useState(false);
  const [isLoading,    setIsLoading]    = useState(false);

  const {
    register,
    handleSubmit,
    watch,
    setError,
    formState: { errors },
  } = useForm<RegisterFormData>();

  const password = watch('password');

  const onSubmit = async (data: RegisterFormData) => {
    setIsLoading(true);
    try {
      await apiPost('/api/v1/auth/register', {
        name:         data.name,
        email:        data.email,
        organization: data.organization,
        password:     data.password,
      });
      success('Account created! Please sign in.');
      router.push(ROUTES.LOGIN);
    } catch (err: any) {
      const msg = err?.message ?? 'Registration failed. Please try again.';
      setError('root', { message: msg });
      toastError(msg);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="w-full max-w-md animate-fade-in">
      <div className="bg-white dark:bg-gray-900 rounded-2xl shadow-xl border border-gray-200 dark:border-gray-800 p-8">
        {/* Logo */}
        <div className="flex justify-center mb-6">
          <div className="flex items-center gap-2">
            <Cpu className="h-8 w-8 text-brand-600" />
            <span className="text-xl font-bold text-gray-900 dark:text-gray-100">RAG Platform</span>
          </div>
        </div>

        <h1 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-1">
          Create your account
        </h1>
        <p className="text-sm text-center text-gray-500 dark:text-gray-400 mb-8">
          Start your 14-day free trial, no credit card required
        </p>

        <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
          <Input
            label="Full name"
            type="text"
            autoComplete="name"
            placeholder="Jane Smith"
            leftIcon={<User className="h-4 w-4" />}
            error={errors.name?.message}
            {...register('name', { required: 'Name is required' })}
          />

          <Input
            label="Work email"
            type="email"
            autoComplete="email"
            placeholder="jane@company.com"
            leftIcon={<Mail className="h-4 w-4" />}
            error={errors.email?.message}
            {...register('email', {
              required: 'Email is required',
              pattern:  { value: /^\S+@\S+\.\S+$/, message: 'Enter a valid email' },
            })}
          />

          <Input
            label="Organization"
            type="text"
            autoComplete="organization"
            placeholder="Acme Corp"
            leftIcon={<Building2 className="h-4 w-4" />}
            error={errors.organization?.message}
            {...register('organization', { required: 'Organization is required' })}
          />

          <Input
            label="Password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="new-password"
            placeholder="At least 8 characters"
            leftIcon={<Lock className="h-4 w-4" />}
            rightIcon={
              <button
                type="button"
                onClick={() => setShowPassword((s) => !s)}
                className="text-gray-400 hover:text-gray-600"
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            }
            error={errors.password?.message}
            {...register('password', {
              required:  'Password is required',
              minLength: { value: 8, message: 'At least 8 characters' },
              pattern:   {
                value:   /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)/,
                message: 'Must include upper, lower and a number',
              },
            })}
          />

          <Input
            label="Confirm password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="new-password"
            placeholder="••••••••"
            leftIcon={<Lock className="h-4 w-4" />}
            error={errors.confirmPassword?.message}
            {...register('confirmPassword', {
              required: 'Please confirm your password',
              validate: (v) => v === password || 'Passwords do not match',
            })}
          />

          {errors.root && (
            <p className="text-sm text-red-600 dark:text-red-400 text-center">
              {errors.root.message}
            </p>
          )}

          <Button type="submit" fullWidth loading={isLoading} size="md">
            Create account
          </Button>
        </form>

        <p className="text-xs text-center text-gray-400 mt-4">
          By signing up, you agree to our{' '}
          <a href="#" className="underline hover:text-gray-600">Terms of Service</a>{' '}
          and{' '}
          <a href="#" className="underline hover:text-gray-600">Privacy Policy</a>.
        </p>

        <p className="text-sm text-center text-gray-500 dark:text-gray-400 mt-4">
          Already have an account?{' '}
          <Link href={ROUTES.LOGIN} className="text-brand-600 hover:text-brand-700 font-medium">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
