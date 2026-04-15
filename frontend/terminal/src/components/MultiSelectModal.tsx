import React from 'react';
import {Box, Text} from 'ink';

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
			<Text dimColor>{'\u2191\u2193'} navigate{'  '}space 체크{'  '}enter 완료{'  '}esc 취소</Text>
		</Box>
	);
}
