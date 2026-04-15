import React from 'react';
import {Box, Text} from 'ink';

/**
 * Custom component: a checkbox-style multi-select modal rendered inline in the terminal.
 * The parent (App) owns cursor/checked state; key handling lives in App's useInput handler.
 * Used for flows where the user must pick one or more options (e.g. custom API auth methods).
 */
export type MultiSelectOption = {
	value: string;
	label: string;
	description?: string;
};

export function MultiSelectModal({
	title,
	options,
	cursorIndex,
	checkedValues,
}: {
	title: string;
	options: MultiSelectOption[];
	cursorIndex: number;
	checkedValues: Set<string>;
}): React.JSX.Element {
	return (
		<Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1} marginTop={1}>
			<Text bold color="cyan">{title}</Text>
			<Text> </Text>
			{options.map((opt, i) => {
				const isCursor = i === cursorIndex;
				const isChecked = checkedValues.has(opt.value);
				return (
					<Box key={opt.value} flexDirection="row">
						<Text color={isCursor ? 'cyan' : undefined} bold={isCursor}>
							{isCursor ? '\u276F ' : '  '}
							<Text color={isChecked ? 'green' : 'gray'}>{isChecked ? '[x]' : '[ ]'}</Text>
							{' '}
							<Text color={isCursor ? 'cyan' : undefined}>{opt.label}</Text>
						</Text>
						{opt.description ? <Text dimColor>{'  '}{opt.description}</Text> : null}
					</Box>
				);
			})}
			<Text> </Text>
			{/* ↑↓ navigate  space check/uncheck  enter confirm  esc cancel (체크/해제) */}
			<Text dimColor>{'\u2191\u2193'} navigate{'  '}space 체크{'  '}enter 완료{'  '}esc 취소</Text>
		</Box>
	);
}
