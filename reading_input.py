"""Read-only dialogue input: no editor selection/copy, while keeping scrolling."""


def make_dialogue_display(widget):
    # Tk's Text class still selects and copies when state='disabled'. These
    # dialogue surfaces use application navigation instead of editor bindings.
    widget.bindtags(tuple(tag for tag in widget.bindtags() if tag != 'Text'))
    widget.configure(cursor='hand2', takefocus=False)

    def scroll(event):
        if event.delta:
            widget.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    widget.bind('<MouseWheel>', scroll)
    widget.bind('<Button-4>', lambda _: (widget.yview_scroll(-1, 'units'), 'break')[-1])
    widget.bind('<Button-5>', lambda _: (widget.yview_scroll(1, 'units'), 'break')[-1])


def bind_advance_area(widget, advance):
    widget.bind('<ButtonPress-1>', lambda _: 'break')

    def release(_):
        advance()
        return 'break'

    widget.bind('<ButtonRelease-1>', release)
