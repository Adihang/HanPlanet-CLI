import React from 'react';
import {Box, Text} from 'ink';
import TextInput from 'ink-text-input';

import {useTheme} from '../theme/ThemeContext.js';
import {Spinner} from './Spinner.js';

const noop = (): void => {};

export function PromptInput({
	busy,
	input,
	setInput,
	onSubmit,
	toolName,
	suppressSubmit,
	statusLabel,
	focus = true,
}: {
	busy: boolean;
	input: string;
	setInput: (value: string) => void;
	onSubmit: (value: string) => void;
	toolName?: string;
	suppressSubmit?: boolean;
	statusLabel?: string;
	focus?: boolean;
}): React.JSX.Element {
	const {theme} = useTheme();

	return (
		<Box flexDirection="column">
			{busy ? (
				// Custom: removed the upstream braille-frame animation row ("Agent is working…").
				// The Spinner component now handles the animated indicator on its own.
				// (업스트림의 braille 프레임 애니메이션 줄 제거 — Spinner 컴포넌트가 직접 처리)
				<Box marginBottom={0}>
					<Spinner label={statusLabel ?? (toolName ? `Running ${toolName}...` : undefined)} />
				</Box>
			) : null}
			<Box>
				<Text color={theme.colors.primary} bold>{busy ? '… ' : '> '}</Text>
				<TextInput
					value={input}
					focus={focus}
					onChange={setInput}
					onSubmit={suppressSubmit || busy ? noop : onSubmit}
				/>
			</Box>
		</Box>
	);
}
